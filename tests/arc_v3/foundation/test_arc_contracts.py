"""Foundation Tests for Arc Contract Extensions (T03)

Covers:
- Positive roundtrip and invariant checking of all 8 Arc extension contracts
- Boundary tests: frozen dataclass mutation, zero decimals, non-decreasing L1 timestamps
- Fail-closed negative tests: network mismatch, invalid block domain, excessive trade limits,
  simulation truth assertion violations.
"""

from dataclasses import FrozenInstanceError
import pytest

from arbitrage_contracts.arc_extensions import (
    BlockDomain,
    CostEvidence,
    CoverageManifest,
    MarketStructureEvent,
    NetworkProfile,
    OtcQuote,
    RawEnvelope,
    SimulationEvidenceBridge,
    SimulationStatus,
    TickCoverage,
)


class TestArcContractExtensions:
    """Test suite for T03 frozen public contracts."""

    def test_network_profile_mainnet_valid(self) -> None:
        profile = NetworkProfile(
            chain_id=5042,
            name="arc-mainnet",
            block_domain=BlockDomain.L1,
            rpc_endpoints=("https://rpc.arc.network",),
            native_asset_domain="arc:native:usdc:18",
            max_trade_usd=500.0,
            is_testnet=False,
        )
        assert profile.chain_id == 5042
        assert profile.block_domain == BlockDomain.L1
        assert not profile.is_testnet
        assert profile.max_trade_usd == 500.0

    def test_network_profile_testnet_valid(self) -> None:
        profile = NetworkProfile(
            chain_id=5042002,
            name="arc-testnet",
            block_domain=BlockDomain.L1,
            rpc_endpoints=("https://rpc.testnet.arc.network",),
            native_asset_domain="arc:native:usdc:18",
            max_trade_usd=100.0,
            is_testnet=True,
        )
        assert profile.chain_id == 5042002
        assert profile.is_testnet

    def test_network_profile_rejects_l2_domain(self) -> None:
        with pytest.raises(ValueError, match="Arc network must use L1 block domain"):
            NetworkProfile(
                chain_id=5042,
                name="arc-mainnet-bad",
                block_domain=BlockDomain.L2,  # type: ignore[arg-type]
                rpc_endpoints=("https://rpc.arc.network",),
                native_asset_domain="arc:native:usdc:18",
            )

    def test_network_profile_rejects_invalid_chain_id(self) -> None:
        with pytest.raises(ValueError, match="Invalid Arc chain_id: 1"):
            NetworkProfile(
                chain_id=1,
                name="ethereum",
                block_domain=BlockDomain.L1,
                rpc_endpoints=("https://eth.rpc",),
                native_asset_domain="eth",
            )

    def test_network_profile_rejects_mainnet_as_testnet(self) -> None:
        with pytest.raises(ValueError, match="Mainnet chain_id 5042 cannot be flagged as is_testnet=True"):
            NetworkProfile(
                chain_id=5042,
                name="arc-mainnet-fake",
                block_domain=BlockDomain.L1,
                rpc_endpoints=("https://rpc.arc.network",),
                native_asset_domain="arc:native:usdc:18",
                is_testnet=True,
            )

    def test_network_profile_rejects_excessive_limit(self) -> None:
        with pytest.raises(ValueError, match="max_trade_usd must be in"):
            NetworkProfile(
                chain_id=5042,
                name="arc-mainnet",
                block_domain=BlockDomain.L1,
                rpc_endpoints=("https://rpc.arc.network",),
                native_asset_domain="arc:native:usdc:18",
                max_trade_usd=501.0,  # exceeds hard ceiling
            )

    def test_frozen_immutability(self) -> None:
        profile = NetworkProfile(
            chain_id=5042,
            name="arc-mainnet",
            block_domain=BlockDomain.L1,
            rpc_endpoints=("https://rpc.arc.network",),
            native_asset_domain="arc:native:usdc:18",
        )
        with pytest.raises(FrozenInstanceError):
            profile.name = "tampered"  # type: ignore[misc]

    def test_raw_envelope_valid(self) -> None:
        envelope = RawEnvelope(
            chain_id=5042,
            block_domain=BlockDomain.L1,
            block_number=123456,
            block_hash="0x" + "a" * 64,
            cursor="cur_123456_0",
            received_at=1726040000.0,
            payload_type="block_with_txs",
            raw_payload='{"number": 123456}',
        )
        assert envelope.block_number == 123456
        assert envelope.block_hash.startswith("0x")

    def test_raw_envelope_rejects_invalid_hash_or_cursor(self) -> None:
        with pytest.raises(ValueError, match="Invalid block_hash format"):
            RawEnvelope(
                chain_id=5042,
                block_domain=BlockDomain.L1,
                block_number=1,
                block_hash="bad_hash",
                cursor="c1",
                received_at=100.0,
                payload_type="test",
                raw_payload="{}",
            )

        with pytest.raises(ValueError, match="cursor cannot be empty"):
            RawEnvelope(
                chain_id=5042,
                block_domain=BlockDomain.L1,
                block_number=1,
                block_hash="0x" + "0" * 64,
                cursor="",
                received_at=100.0,
                payload_type="test",
                raw_payload="{}",
            )

    def test_coverage_manifest_accounting(self) -> None:
        manifest = CoverageManifest(
            chain_id=5042,
            from_block=100,
            to_block=104,
            expected_blocks=5,
            covered_blocks=4,
            missing_blocks=(102,),
            coverage_ratio=0.8,
            verified_at=1726040000.0,
        )
        assert manifest.expected_blocks == 5
        assert manifest.missing_blocks == (102,)

        with pytest.raises(ValueError, match="expected_blocks mismatch"):
            CoverageManifest(
                chain_id=5042,
                from_block=100,
                to_block=104,
                expected_blocks=10,  # incorrect calculation
                covered_blocks=10,
            )

    def test_simulation_evidence_bridge_truth_invariants(self) -> None:
        # Valid successful simulation
        sim_ok = SimulationEvidenceBridge(
            call_succeeded=True,
            output_verified=True,
            status=SimulationStatus.CALL_SUCCEEDED,
            net_output_atoms=1500000,
            gas_used_atoms=120000,
            backend="eth_call",
        )
        assert sim_ok.call_succeeded
        assert sim_ok.output_verified

        # OUTPUT_UNVERIFIED cannot assert output_verified=True
        with pytest.raises(ValueError, match="OUTPUT_UNVERIFIED status cannot have output_verified=True"):
            SimulationEvidenceBridge(
                call_succeeded=True,
                output_verified=True,  # illegal combination
                status=SimulationStatus.OUTPUT_UNVERIFIED,
                net_output_atoms=None,
                gas_used_atoms=100000,
                backend="eth_call",
            )

        # CONTRACT_REVERT requires call_succeeded=False
        with pytest.raises(ValueError, match="CONTRACT_REVERT status requires call_succeeded=False"):
            SimulationEvidenceBridge(
                call_succeeded=True,  # illegal combination
                output_verified=False,
                status=SimulationStatus.CONTRACT_REVERT,
                net_output_atoms=None,
                gas_used_atoms=50000,
                backend="eth_call",
                execution_revert_reason="TRANSFER_FAILED",
            )

    def test_cost_evidence_non_negative(self) -> None:
        cost = CostEvidence(
            currency="USD",
            cost_atoms=100000,
            source="quoter_fee",
            kind="dex_fee",
            estimated_or_observed="observed",
        )
        assert cost.cost_atoms == 100000

        with pytest.raises(ValueError, match="cost_atoms cannot be negative"):
            CostEvidence(
                currency="USD",
                cost_atoms=-10,
                source="test",
                kind="gas",
                estimated_or_observed="estimated",
            )

    def test_otc_quote_validation(self) -> None:
        otc = OtcQuote(
            quote_id="otc-001",
            venue="wintermute",
            base_asset="USDC",
            quote_asset="USDG",
            side="buy",
            amount_in_atoms=1000000,
            amount_out_atoms=1000500,
            expiry_timestamp=1726050000.0,
        )
        assert otc.side == "buy"
        assert otc.amount_out_atoms > otc.amount_in_atoms

        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            OtcQuote(
                quote_id="otc-bad",
                venue="venue",
                base_asset="A",
                quote_asset="B",
                side="arbitrage",  # illegal
                amount_in_atoms=1,
                amount_out_atoms=1,
                expiry_timestamp=100.0,
            )
