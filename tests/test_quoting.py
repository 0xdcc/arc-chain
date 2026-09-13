"""Tests for quoting engine and historical replay adapter (M4).

Verifies:
1. 30-record immutable replay log is fully and strictly consumed without surplus or deficit.
2. 8 quoting tiers produce exact 7 CONTRACT_REVERT and 1 NODE_LIMITATION.
3. Invariant check: failed quotes strictly have amount_out is None and delta_atoms is None.
4. Tampering defenses: modified calldata, altered order, block mismatch, excess/deficit records.
5. Synthetic positive control group: verifies Quoter can produce QUOTED status on valid outputs.
6. Non-duplication of fees: Quoter output already includes fees; no double-deduction.
7. Price safety: Missing USD price never defaults to $1.00 USD.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from research.market_data.types import (
    CandidateRoute,
    PoolIdentity,
    QuoteResult,
    QuoteStatus,
    RouteHop,
    TokenAmount,
    TokenIdentity,
)
from research.quoting import (
    Quoter,
    ReplayAdapter,
    build_canonical_route_a,
    build_canonical_route_b,
    classify_rpc_error,
    estimate_usd_profit,
    run_historical_replay,
)


class TestHistoricalReplayExactParity:
    """Tests exact replay execution against data/live-rpc.jsonl."""

    def test_thirty_records_fully_consumed_and_classified(self) -> None:
        """30 immutable records produce 8 tiers: 7 CONTRACT_REVERT + 1 NODE_LIMITATION."""
        adapter = ReplayAdapter()
        assert adapter.total_count == 30
        assert adapter.index == 0

        results = run_historical_replay(adapter)

        # 1. Verification of consumption
        assert len(results) == 8
        assert adapter.is_finished() is True
        assert adapter.consumed_count() == 30
        assert adapter.remaining_count() == 0

        # 2. Strict status classification: first 7 are reverts, 8th is node limitation
        for i in range(7):
            assert results[i].status == QuoteStatus.CONTRACT_REVERT, f"Tier {i} mismatch"
            assert results[i].amount_out is None, f"Tier {i} amount_out must be None"
            assert results[i].delta_atoms is None, f"Tier {i} delta_atoms must be None"

        assert results[7].status == QuoteStatus.NODE_LIMITATION
        assert results[7].amount_out is None
        assert results[7].delta_atoms is None
        assert "Archive requests require a personal token" in str(results[7].error_message)

    def test_replay_adapter_verify_complete_method(self) -> None:
        """Calling verify_complete() before all records are consumed raises RuntimeError."""
        adapter = ReplayAdapter()
        adapter.call("eth_chainId")
        with pytest.raises(RuntimeError, match="Replay incomplete"):
            adapter.verify_complete()

    def test_zero_consumed_verify_complete_rejected(self) -> None:
        """Calling verify_complete() before consuming ANY records strictly raises RuntimeError."""
        adapter = ReplayAdapter()
        assert adapter.consumed_count() == 0
        assert adapter.total_count == 30
        assert adapter.is_finished() is False
        with pytest.raises(
            RuntimeError,
            match=r"Replay incomplete: 0 of 30 records consumed",
        ):
            adapter.verify_complete()

    def test_empty_fixture_reasonable_semantics(self) -> None:
        """Empty fixture semantics: verify_complete passes cleanly, calls fail exhausted."""
        empty_adapter = ReplayAdapter([])
        assert empty_adapter.total_count == 0
        assert empty_adapter.consumed_count() == 0
        assert empty_adapter.remaining_count() == 0
        assert empty_adapter.is_finished() is True

        # Reasonable semantic: zero unconsumed records remain, verify_complete passes cleanly
        empty_adapter.verify_complete()

        # Any replay call on empty adapter immediately fails closed with exhausted error
        with pytest.raises(RuntimeError, match=r"Replay exhausted at index 0"):
            empty_adapter.call("eth_chainId")

        # Historical session replay on empty adapter also fails closed at record 0
        quoter = Quoter(rpc_client=empty_adapter)
        with pytest.raises(RuntimeError, match=r"Replay exhausted at index 0"):
            quoter.replay_historical_session(empty_adapter)

    def test_session_mid_failure_fails_closed_without_masking(self) -> None:
        """Exceptions midway through replay fail closed immediately without masking."""
        adapter = ReplayAdapter()
        records = adapter.records
        records[5] = copy.deepcopy(records[5])
        records[5]["params"] = ["tampered_params"]
        tampered_adapter = ReplayAdapter(records)

        quoter = Quoter(rpc_client=tampered_adapter)
        # Session fails closed immediately with exact root cause error, never returns QuoteResults
        with pytest.raises(RuntimeError, match="Replay params mismatch at index 5"):
            quoter.replay_historical_session(tampered_adapter)

        # Unconsumed records remain; calling verify_complete explicitly reports incomplete
        assert tampered_adapter.consumed_count() == 5
        with pytest.raises(RuntimeError, match=r"Replay incomplete: 5 of 30 records consumed"):
            tampered_adapter.verify_complete()


class TestReplayAdapterTamperDefenses:
    """Tests tamper-resistance and strict validation of ReplayAdapter."""

    def test_tampered_calldata_rejected(self) -> None:
        """Tampering calldata at any step immediately raises RuntimeError."""
        adapter = ReplayAdapter()
        adapter.call("eth_chainId")
        adapter.call("eth_blockNumber")
        adapter.call("eth_getBlockByNumber", ["0x378a957", False])
        adapter.call("eth_gasPrice")

        # Record 4 expects StateView.getSlot0 for p_cc_pons
        tampered_data = "0xc815641c" + "ff" * 32
        with pytest.raises(RuntimeError, match="Replay params mismatch"):
            adapter.call(
                "eth_call",
                [{"to": "0xF3334192D15450CdD385c8B70e03f9A6bD9E673b", "data": tampered_data}, "0x378a957"],
                block_identifier="0x378a957",
            )

    def test_tampered_block_identifier_rejected(self) -> None:
        """Calling with different block identifier raises RuntimeError."""
        adapter = ReplayAdapter()
        adapter.call("eth_chainId")
        adapter.call("eth_blockNumber")
        with pytest.raises(RuntimeError, match="Replay params mismatch|Replay block_identifier mismatch"):
            adapter.call("eth_getBlockByNumber", ["0x9999999", False])

    def test_tampered_block_identifier_isolated_rejection(self) -> None:
        """Pure block_identifier mismatch: method & params match 100%, only block differs."""
        adapter = ReplayAdapter()
        adapter.call("eth_chainId")
        adapter.call("eth_blockNumber")
        adapter.call("eth_getBlockByNumber", ["0x378a957", False])
        adapter.call("eth_gasPrice")

        # Record 4 expects block_identifier="0x378a957"
        rec4 = adapter.records[4]

        # Case 1: method & params match 100%, wrong non-null block_identifier
        with pytest.raises(
            RuntimeError,
            match=(
                r"Replay block_identifier mismatch at index 4 for method 'eth_call': "
                r"expected '0x378a957', got '0x9999999'"
            ),
        ):
            adapter.call(
                method=rec4["method"],
                params=rec4["params"],
                block_identifier="0x9999999",
            )

        # Case 2: method & params match 100%, missing block_identifier (None)
        adapter_none = ReplayAdapter()
        adapter_none.call("eth_chainId")
        adapter_none.call("eth_blockNumber")
        adapter_none.call("eth_getBlockByNumber", ["0x378a957", False])
        adapter_none.call("eth_gasPrice")
        with pytest.raises(
            RuntimeError,
            match=(
                r"Replay block_identifier mismatch at index 4 for method 'eth_call': "
                r"expected '0x378a957', got 'None'"
            ),
        ):
            adapter_none.call(
                method=rec4["method"],
                params=rec4["params"],
                block_identifier=None,
            )

        # Case 3: record 0 expects block_identifier=None, passed non-null block_identifier
        adapter_zero = ReplayAdapter()
        rec0 = adapter_zero.records[0]
        with pytest.raises(
            RuntimeError,
            match=(
                r"Replay block_identifier mismatch at index 0 for method 'eth_chainId': "
                r"expected 'None', got '0x378a957'"
            ),
        ):
            adapter_zero.call(
                method=rec0["method"],
                params=rec0.get("params"),
                block_identifier="0x378a957",
            )

    def test_out_of_order_call_rejected(self) -> None:
        """Calling methods out of order raises RuntimeError."""
        adapter = ReplayAdapter()
        # Record 0 is eth_chainId, but we call eth_blockNumber
        with pytest.raises(RuntimeError, match="Replay method mismatch"):
            adapter.call("eth_blockNumber")

    def test_deficit_records_rejected(self) -> None:
        """Truncated replay log missing calls raises RuntimeError when finished prematurely."""
        adapter = ReplayAdapter()
        truncated_records = adapter.records[:10]  # Only 10 records
        trunc_adapter = ReplayAdapter(truncated_records)

        with pytest.raises(RuntimeError, match="Replay exhausted at index 10"):
            run_historical_replay(trunc_adapter)

    def test_excess_records_detected_by_verify_complete(self) -> None:
        """Replay log with extra records fails verify_complete()."""
        adapter = ReplayAdapter()
        extra_records = adapter.records + [copy.deepcopy(adapter.records[-1])]
        extra_adapter = ReplayAdapter(extra_records)

        quoter = Quoter(rpc_client=extra_adapter)
        with pytest.raises(RuntimeError, match="Replay incomplete"):
            quoter.replay_historical_session(extra_adapter)

        assert extra_adapter.is_finished() is False


class TestSyntheticQuoteControlGroup:
    """Tests synthetic positive control group to verify Quoter is not an all-reject stub."""

    def test_synthetic_quote_produces_quoted_status(self) -> None:
        """Synthetic mock RPC returns valid Quoter output and produces QuoteStatus.QUOTED."""
        token_a = TokenIdentity(chain_id=4663, address="0x" + "11" * 20, decimals=18, symbol="WETH")
        token_b = TokenIdentity(chain_id=4663, address="0x" + "22" * 20, decimals=6, symbol="USDG")

        pool = PoolIdentity(
            chain_id=4663,
            protocol="uniswap_v3",
            pool_id="0x" + "33" * 20,
            token0=token_a.address.lower(),
            token1=token_b.address.lower(),
            fee_bps=5.0,
            tick_spacing=10,
        )
        hop = RouteHop(pool=pool, token_in=token_a, token_out=token_b)

        # Mock RPC simulating successful V3 quoter response: (amountOut=2500e6, gas=120000)
        from eth_abi import encode as abi_encode

        mock_rpc = MagicMock()
        encoded_ret = abi_encode(["uint256", "uint160[]", "uint32[]", "uint256"], [2500_000_000, [], [], 120_000])
        mock_rpc.call.return_value = {"result": "0x" + encoded_ret.hex()}

        quoter = Quoter(rpc_client=mock_rpc)
        amount_in = TokenAmount(token=token_a, atoms=10**18)  # 1 WETH

        res = quoter.quote_hop(hop=hop, amount_in=amount_in, quote_mode="synthetic")

        assert res.status == QuoteStatus.QUOTED
        assert res.amount_out is not None
        assert res.amount_out.atoms == 2500_000_000
        assert res.amount_out.token.symbol == "USDG"
        assert res.quote_mode == "synthetic"
        assert res.gas_estimate == 120_000


class TestQuoterFeeAndPriceSafety:
    """Tests fee non-duplication and price oracle fallback safety."""

    def test_quoter_fees_not_double_deducted(self) -> None:
        """Quoter output already incorporates AMM fee; external fees must not be re-deducted."""
        token = TokenIdentity(chain_id=4663, address="0x" + "11" * 20, decimals=18, symbol="WETH")
        amount_in = TokenAmount(token=token, atoms=10**18)
        amount_out = TokenAmount(token=token, atoms=10**18 + 10**16)  # +0.01 WETH gross

        # Quoter delta calculation
        delta_atoms = amount_out.atoms - amount_in.atoms
        assert delta_atoms == 10**16  # Exactly 0.01 WETH

        quote_res = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=delta_atoms,
        )

        # No double deduction of fee: net token change is delta_atoms
        profit_usd = estimate_usd_profit(
            quote_result=quote_res,
            token_usd_price=Decimal(2500),
        )
        # 0.01 WETH * $2500 = $25.00
        assert profit_usd == Decimal("25.00")

    def test_missing_price_never_defaults_to_one_dollar(self) -> None:
        """When base USD price is missing or None, estimate_usd_profit returns profit_usd=None."""
        token = TokenIdentity(chain_id=4663, address="0x" + "11" * 20, decimals=18, symbol="WETH")
        amount_in = TokenAmount(token=token, atoms=10**18)
        amount_out = TokenAmount(token=token, atoms=10**18 + 10**16)
        delta_atoms = 10**16

        quote_res = QuoteResult(
            status=QuoteStatus.QUOTED,
            amount_in=amount_in,
            amount_out=amount_out,
            delta_atoms=delta_atoms,
        )

        profit_usd = estimate_usd_profit(
            quote_result=quote_res,
            token_usd_price=None,  # Missing price!
        )
        # MUST NEVER DEFAULT TO $1.00 USD
        assert profit_usd is None
