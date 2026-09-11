"""Comprehensive unit and regression test suite for W5-C pure planning and funds policy."""

from __future__ import annotations

import ast
import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest

from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import (
    GasEvidence,
    GasEvidenceKind,
    HopQuote,
    HopRef,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)
from arbitrage_contracts.state import StateVersion
from atomic_execution.inputs import (
    ROBINHOOD_CHAIN_ID,
    USDG_ADDRESS_4663,
    WETH_ADDRESS_4663,
    ZERO_ADDRESS,
)
from atomic_execution.models import (
    CandidateOpportunity,
    ValidatedCandidate,
)
from atomic_execution.planning import (
    CANONICAL_UNIVERSAL_ROUTER,
    DEFAULT_DEADLINE_SECONDS,
    ExecutionPlan,
    PlanningError,
    build_execution_plan,
    evaluate_plan_assembly,
)
from atomic_execution.policy import (
    DEFAULT_SLIPPAGE_BPS,
    MAX_PROBE_LOSS_BUDGET_USD,
    MAX_TRADE_AMOUNT_USD,
    MIN_SLIPPAGE_BPS,
    ExcessiveAmountError,
    ExecutionPolicy,
    PolicyDecision,
    PolicyMode,
    PolicyRejectionReason,
    PolicyViolationError,
    calculate_output_floor,
    enforce_execution_policy,
    evaluate_execution_policy,
    validate_positive_decimal,
)

FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "atomic_execution" / "v1"
)

TEST_STATE_HASH = "0x" + "a" * 64
TEST_MID_TOKEN = "0x1111111111111111111111111111111111111111"
TEST_POOL_1 = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEST_POOL_2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _make_asset(address: str, chain_id: int = ROBINHOOD_CHAIN_ID) -> AssetRef:
    return AssetRef.erc20(TokenKey(chain_id, address))


def _make_pool_key(pool_id: str, protocol_id: str = "uniswap_v3") -> PoolKey:
    return PoolKey(
        chain_id=ROBINHOOD_CHAIN_ID,
        protocol_id=protocol_id,
        pool_id=pool_id,
        pool_id_kind="address",
        venue_address="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        venue_kind="factory",
    )


def _make_valid_2hop_bundle(
    *,
    base_address: str = WETH_ADDRESS_4663,
    decimals: int = 18,
    amount_in_atoms: int = 100_000_000_000_000_000,  # 0.1 WETH
    amount_out_atoms: int = 100_500_000_000_000_000,  # 0.1005 WETH
    status: QuoteStatus = QuoteStatus.QUOTED,
    gas_evidence: GasEvidence | None = None,
    hop_fee_model: FeeModel | None = None,
) -> tuple[RouteRef, QuoteEvidence, StateVersion]:
    base = _make_asset(base_address)
    mid = _make_asset(TEST_MID_TOKEN)
    pk1 = _make_pool_key(TEST_POOL_1)
    pk2 = _make_pool_key(TEST_POOL_2)

    fee1 = hop_fee_model if hop_fee_model is not None else FeeModel.static(500)
    fee2 = FeeModel.static(500)

    desc1 = PoolDescriptor(
        key=pk1,
        currency0=base,
        currency1=mid,
        fee_model=fee1,
        hooks=ZERO_ADDRESS,
    )
    desc2 = PoolDescriptor(
        key=pk2,
        currency0=base,
        currency1=mid,
        fee_model=fee2,
        hooks=ZERO_ADDRESS,
    )

    hop1 = HopRef(pk1, base, mid, direction="zero_for_one", pool_descriptor=desc1)
    hop2 = HopRef(pk2, mid, base, direction="one_for_zero", pool_descriptor=desc2)

    route = RouteRef(ROBINHOOD_CHAIN_ID, base, (hop1, hop2))

    amt_in = Amount(base, amount_in_atoms, decimals)
    amt_out = Amount(base, amount_out_atoms, decimals) if status == QuoteStatus.QUOTED else None

    mid_amt = Amount(mid, 200_000_000, decimals) if status == QuoteStatus.QUOTED else None
    hq1 = HopQuote(0, pk1, base, mid, amt_in, mid_amt, status=status, fee_model=fee1)
    hq2 = HopQuote(1, pk2, mid, base, Amount(mid, 200_000_000, decimals), amt_out, status=status, fee_model=fee2)

    quote = QuoteEvidence(
        quote_id="quote:valid-bundle",
        route_ref=route,
        amount_in=amt_in,
        amount_out=amt_out,
        hop_quotes=(hq1, hq2),
        state_version_ref=TEST_STATE_HASH,
        status=status,
        gas_evidence=gas_evidence,
    )

    state = StateVersion(
        chain_id=ROBINHOOD_CHAIN_ID,
        block_number=100,
        block_hash=TEST_STATE_HASH,
        received_at_ms=1000,
        complete_through_block=100,
        completeness="ready",
    )
    return route, quote, state


# ==============================================================================
# Fixture Test Matrix Execution
# ==============================================================================


def test_fixtures_policy_cases_file_integrity() -> None:
    """Fixture JSONL file exists and contains valid test cases."""
    fixture_file = FIXTURES_DIR / "policy-cases.jsonl"
    assert fixture_file.is_file(), f"Missing fixture file {fixture_file}"

    lines = [line.strip() for line in fixture_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 25, f"Expected at least 25 policy test cases, got {len(lines)}"

    for idx, raw in enumerate(lines):
        record = json.loads(raw)
        assert "case_id" in record, f"Line {idx} missing case_id"
        assert "category" in record, f"Line {idx} missing category"
        assert record["category"] in ("C06", "C07", "C08", "C09")
        assert "expected" in record, f"Line {idx} missing expected"


def test_fixtures_policy_cases_execution() -> None:
    """Execute all JSONL policy fixture test cases against funds policy calculator."""
    fixture_file = FIXTURES_DIR / "policy-cases.jsonl"
    lines = [json.loads(line) for line in fixture_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    for case in lines:
        case_id = case["case_id"]
        policy_cfg = case["policy"]

        price = (
            Decimal(case["base_asset_usd_price"])
            if case["base_asset_usd_price"] not in ("NaN", "Infinity") and case["base_asset_usd_price"] is not None
            else case["base_asset_usd_price"]
        )
        gas_usd = Decimal(case["conservative_gas_usd"]) if case.get("conservative_gas_usd") is not None else None

        gas_evidence = None
        if case.get("has_gas_evidence"):
            gas_evidence = GasEvidence(gas_kind=case.get("gas_kind", "rpc_estimate"))

        hop_quotes: tuple[HopQuote, ...] = ()
        if case.get("has_unknown_fee_hop"):
            pk = _make_pool_key(TEST_POOL_1)
            hq = HopQuote(
                0, pk, _make_asset(WETH_ADDRESS_4663), _make_asset(TEST_MID_TOKEN),
                Amount(_make_asset(WETH_ADDRESS_4663), case["amount_in"], case["decimals"]),
                Amount(_make_asset(TEST_MID_TOKEN), 1000, case["decimals"]),
                fee_model=FeeModel(kind="unknown"),
            )
            hop_quotes = (hq,)

        # Attempt to construct policy; if invalid parameters, policy constructor raises
        try:
            policy = ExecutionPolicy.from_dict(policy_cfg)
        except (ValueError, ExcessiveAmountError, TypeError) as exc:
            if case["expected"] == "excessive_amount_error":
                assert isinstance(exc, ExcessiveAmountError)
                continue
            elif case["expected"] == "rejected":
                assert case["expected_reason"] in (
                    PolicyRejectionReason.INVALID_SLIPPAGE,
                    PolicyRejectionReason.PROBE_MODE_DISABLED,
                    PolicyRejectionReason.PROBE_BUDGET_EXCEEDED,
                )
                continue
            else:
                raise

        if case["expected"] == "excessive_amount_error":
            with pytest.raises(ExcessiveAmountError):
                evaluate_execution_policy(
                    amount_in=case["amount_in"],
                    expected_out=case["expected_out"],
                    decimals=case["decimals"],
                    base_asset_usd_price=price,
                    conservative_gas_usd=gas_usd,
                    policy=policy,
                    gas_evidence=gas_evidence,
                    hop_quotes=hop_quotes,
                )
        elif case["expected"] == "approved":
            decision = evaluate_execution_policy(
                amount_in=case["amount_in"],
                expected_out=case["expected_out"],
                decimals=case["decimals"],
                base_asset_usd_price=price,
                conservative_gas_usd=gas_usd,
                policy=policy,
                gas_evidence=gas_evidence,
                hop_quotes=hop_quotes,
            )
            assert decision.approved is True, f"[{case_id}] Expected approved but got {decision.reason}: {decision.message}"
            assert decision.output_floor > 0, f"[{case_id}] Output floor must be positive"
            assert decision.output_floor <= case["expected_out"], f"[{case_id}] Floor exceeds quote"
            if "expected_gas_atoms" in case:
                assert decision.gas_atoms == case["expected_gas_atoms"], f"[{case_id}] Gas atoms mismatch"
            if "expected_net_atoms" in case:
                assert decision.net_atoms == case["expected_net_atoms"], f"[{case_id}] Net atoms mismatch"
        else:
            decision = evaluate_execution_policy(
                amount_in=case["amount_in"],
                expected_out=case["expected_out"],
                decimals=case["decimals"],
                base_asset_usd_price=price,
                conservative_gas_usd=gas_usd,
                policy=policy,
                gas_evidence=gas_evidence,
                hop_quotes=hop_quotes,
            )
            assert decision.approved is False, f"[{case_id}] Expected rejection, but approved"
            assert str(decision.reason) == case["expected_reason"], (
                f"[{case_id}] Expected reason {case['expected_reason']}, got {decision.reason}"
            )


# ==============================================================================
# Category C06 Tests: 费用完整性与未知拦截
# ==============================================================================


def test_c06_price_fail_closed_on_zero_negative_nan_inf_none() -> None:
    """Asset price must be positive finite Decimal; nonpositive or nonfinite values fail closed."""
    policy = ExecutionPolicy()
    for bad_price in (0, -1, Decimal("0"), Decimal("-10.5"), "0", "-2500", "NaN", "Infinity", None):
        decision = evaluate_execution_policy(
            amount_in=100_000_000_000_000_000,
            expected_out=100_500_000_000_000_000,
            decimals=18,
            base_asset_usd_price=bad_price,
            conservative_gas_usd=Decimal("0.25"),
            policy=policy,
        )
        assert decision.approved is False
        assert decision.reason in (PolicyRejectionReason.INVALID_PRICE, PolicyRejectionReason.MISSING_PRICE)


def test_c06_price_rejects_floats() -> None:
    """Prices passed as floats must be rejected with TypeError or INVALID_PRICE."""
    policy = ExecutionPolicy()
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=2500.0,  # type: ignore[arg-type]
        conservative_gas_usd=Decimal("0.25"),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason == PolicyRejectionReason.INVALID_PRICE


def test_c06_gas_missing_or_unknown_intercepted() -> None:
    """Missing gas or unknown gas kind must be intercepted fail-closed."""
    policy = ExecutionPolicy()
    # Missing gas entirely
    d1 = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=None,
        gas_evidence=None,
        policy=policy,
    )
    assert d1.approved is False
    assert d1.reason == PolicyRejectionReason.MISSING_GAS

    # Unknown gas kind
    d2 = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=None,
        gas_evidence=GasEvidence(gas_kind="unknown"),
        policy=policy,
    )
    assert d2.approved is False
    assert d2.reason == PolicyRejectionReason.UNKNOWN_GAS


def test_c06_gas_zero_or_negative_intercepted() -> None:
    """Zero or negative gas costs must be intercepted."""
    policy = ExecutionPolicy()
    for bad_gas in (Decimal("0"), Decimal("-0.1"), "0", "-1"):
        decision = evaluate_execution_policy(
            amount_in=100_000_000_000_000_000,
            expected_out=100_500_000_000_000_000,
            decimals=18,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=bad_gas,
            policy=policy,
        )
        assert decision.approved is False
        assert decision.reason == PolicyRejectionReason.INVALID_GAS


def test_c06_usdg_requires_explicit_rate_and_no_one_assumption() -> None:
    """USDG conversion must strictly require explicit price Decimal and never assume 1:1."""
    policy = ExecutionPolicy()
    # None price fails
    d1 = evaluate_execution_policy(
        amount_in=100_000_000,
        expected_out=100_500_000,
        decimals=6,
        base_asset_usd_price=None,
        conservative_gas_usd=Decimal("0.10"),
        policy=policy,
    )
    assert d1.approved is False
    assert d1.reason == PolicyRejectionReason.MISSING_PRICE

    # Explicit 0.998 rate succeeds
    d2 = evaluate_execution_policy(
        amount_in=100_000_000,
        expected_out=100_500_000,
        decimals=6,
        base_asset_usd_price=Decimal("0.998"),
        conservative_gas_usd=Decimal("0.10"),
        policy=policy,
    )
    assert d2.approved is True
    assert d2.base_asset_usd_price == Decimal("0.998")


def test_c06_unknown_fee_models_intercepted() -> None:
    """Hop with unknown fee model fails closed."""
    policy = ExecutionPolicy()
    pk = _make_pool_key(TEST_POOL_1)
    hq = HopQuote(
        0, pk, _make_asset(WETH_ADDRESS_4663), _make_asset(TEST_MID_TOKEN),
        Amount(_make_asset(WETH_ADDRESS_4663), 100_000_000_000_000_000, 18),
        Amount(_make_asset(TEST_MID_TOKEN), 1000, 18),
        fee_model=FeeModel(kind="unknown"),
    )
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        hop_quotes=(hq,),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason == PolicyRejectionReason.UNKNOWN_FEE


# ==============================================================================
# Category C07 Tests: 精度、临界舍入与单扣防线
# ==============================================================================


def test_c07_round_ceiling_precision_conversion() -> None:
    """Upward rounding (ROUND_CEILING) ensures conservative gas assessment."""
    policy = ExecutionPolicy()
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.250000000000000001"),
        policy=policy,
    )
    assert decision.approved is True
    assert decision.gas_atoms == 100000000000001


def test_c07_boundary_1_atom_net_profit_passes() -> None:
    """When quote barely covers principal + gas with exactly 1 atom net profit, it passes."""
    policy = ExecutionPolicy()
    amount_in = 100_000_000_000_000_000
    gas_atoms = 100_000_000_000
    expected_out = amount_in + gas_atoms + 1
    decision = evaluate_execution_policy(
        amount_in=amount_in,
        expected_out=expected_out,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.00025"),  # exactly 100_000_000_000 atoms
        policy=policy,
    )
    assert decision.approved is True
    assert decision.net_atoms == 1


def test_c07_boundary_0_atom_net_profit_fails_strict() -> None:
    """When quote covers principal + gas with 0 atom net profit, strict mode rejects."""
    policy = ExecutionPolicy()
    amount_in = 100_000_000_000_000_000
    gas_atoms = 100_000_000_000
    expected_out = amount_in + gas_atoms  # net = 0
    decision = evaluate_execution_policy(
        amount_in=amount_in,
        expected_out=expected_out,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.00025"),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason in (PolicyRejectionReason.UNPROFITABLE, PolicyRejectionReason.OUTPUT_FLOOR_NOT_MET)


def test_c07_negative_net_profit_fails_strict() -> None:
    """When quote does not cover gas, strict mode rejects as UNPROFITABLE."""
    policy = ExecutionPolicy()
    amount_in = 100_000_000_000_000_000
    expected_out = amount_in + 50_000_000_000  # less than 100_000_000_000 gas
    decision = evaluate_execution_policy(
        amount_in=amount_in,
        expected_out=expected_out,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.00025"),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason == PolicyRejectionReason.UNPROFITABLE


def test_c07_single_deduction_defense_does_not_double_deduct_dex_fee() -> None:
    """Quoter's already deducted DEX pool fee is not deducted a second time."""
    policy = ExecutionPolicy()
    pk = _make_pool_key(TEST_POOL_1)
    hq = HopQuote(
        0, pk, _make_asset(WETH_ADDRESS_4663), _make_asset(TEST_MID_TOKEN),
        Amount(_make_asset(WETH_ADDRESS_4663), 100_000_000_000_000_000, 18),
        Amount(_make_asset(TEST_MID_TOKEN), 1000, 18),
        fee_model=FeeModel.static(3000),
    )
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        hop_quotes=(hq,),
        policy=policy,
    )
    assert decision.approved is True
    assert decision.net_atoms == (100_500_000_000_000_000 - 100_000_000_000_000_000 - decision.gas_atoms)


def test_c07_gas_and_l1_fee_are_strictly_deducted() -> None:
    """Gas and L1 fee components are deducted from gross delta."""
    policy = ExecutionPolicy()
    gas_evidence = GasEvidence(
        gas_kind=GasEvidenceKind.RPC_ESTIMATE,
        gas_units=100_000,
        gas_price_atoms=1_000_000_000,  # 1 gwei
        l1_fee_atoms=20_000_000_000_000,  # 0.00002 ETH L1 fee
    )
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=None,
        gas_evidence=gas_evidence,
        policy=policy,
    )
    assert decision.approved is True
    assert decision.gas_atoms == 120_000_000_000_000
    assert decision.net_atoms == 500_000_000_000_000 - 120_000_000_000_000


# ==============================================================================
# Category C08 Tests: 硬上限与防误杀门禁
# ==============================================================================


def test_c08_amount_usd_hard_cap_boundary_500_usd() -> None:
    """Trade size <= 500.00 USD is permitted."""
    policy = ExecutionPolicy()
    decision = evaluate_execution_policy(
        amount_in=200_000_000_000_000_000,
        expected_out=201_000_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.50"),
        policy=policy,
    )
    assert decision.approved is True
    assert decision.trade_amount_usd == Decimal("500.0")


def test_c08_amount_usd_exceeding_500_raises_excessive_amount_error() -> None:
    """Trade size > 500.00 USD raises ExcessiveAmountError immediately."""
    policy = ExecutionPolicy()
    with pytest.raises(ExcessiveAmountError):
        evaluate_execution_policy(
            amount_in=200_004_000_000_000_000,
            expected_out=201_000_000_000_000_000,
            decimals=18,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=Decimal("0.50"),
            policy=policy,
        )


def test_c08_min_amount_out_must_be_positive() -> None:
    """min_amount_out <= 0 is strictly rejected."""
    policy = ExecutionPolicy()
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=0,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason == PolicyRejectionReason.MIN_AMOUNT_OUT_NON_POSITIVE


def test_c08_min_amount_out_cannot_exceed_expected_out() -> None:
    """Calculated min_amount_out cannot exceed quote expected_out."""
    policy = ExecutionPolicy()
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_000_000_000_000_000,  # no gain, cannot cover gas
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason in (PolicyRejectionReason.OUTPUT_FLOOR_NOT_MET, PolicyRejectionReason.UNPROFITABLE)


def test_c08_anti_false_kill_high_pool_fee_high_gross_margin_passes() -> None:
    """Unique criterion is net profit; proxy fee caps must NOT kill high-fee high-profit trades."""
    policy = ExecutionPolicy()
    pk = _make_pool_key(TEST_POOL_1)
    hq = HopQuote(
        0, pk, _make_asset(WETH_ADDRESS_4663), _make_asset(TEST_MID_TOKEN),
        Amount(_make_asset(WETH_ADDRESS_4663), 100_000_000_000_000_000, 18),
        Amount(_make_asset(TEST_MID_TOKEN), 1000, 18),
        fee_model=FeeModel.static(10000),
    )
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=103_000_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        hop_quotes=(hq,),
        policy=policy,
    )
    assert decision.approved is True
    assert decision.net_atoms > 0


# ==============================================================================
# Category C09 Tests: 资金政策参数化与 Output Floor
# ==============================================================================


def test_c09_slippage_bps_bounds_and_type_check() -> None:
    """Slippage bps must be integer between 1 and 500."""
    p1 = ExecutionPolicy(slippage_bps=1)
    assert p1.slippage_bps == 1
    p2 = ExecutionPolicy(slippage_bps=500)
    assert p2.slippage_bps == 500

    with pytest.raises(ValueError):
        ExecutionPolicy(slippage_bps=0)
    with pytest.raises(ValueError):
        ExecutionPolicy(slippage_bps=-10)
    with pytest.raises(ValueError):
        ExecutionPolicy(slippage_bps=501)
    with pytest.raises(TypeError):
        ExecutionPolicy(slippage_bps=50.5)  # type: ignore[arg-type]


def test_c09_probe_mode_disabled_by_default() -> None:
    """bounded_probe mode without explicit probe_mode_enabled=True raises on policy construction."""
    with pytest.raises(ValueError, match="probe_mode_enabled=True"):
        ExecutionPolicy(mode="bounded_probe", probe_mode_enabled=False)


def test_c09_probe_mode_budget_cap_at_one_usd() -> None:
    """probe_loss_budget_usd cannot exceed 1.0 USD."""
    with pytest.raises(ValueError, match="exceeds maximum allowable"):
        ExecutionPolicy(
            mode="bounded_probe",
            probe_loss_budget_usd=Decimal("1.05"),
            probe_mode_enabled=True,
        )


def test_c09_probe_mode_gas_exceeding_budget_rejected() -> None:
    """In probe mode, gas alone must fit within the approved loss budget."""
    policy = ExecutionPolicy(
        mode="bounded_probe",
        probe_loss_budget_usd=Decimal("0.20"),
        probe_mode_enabled=True,
    )
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.50"),
        policy=policy,
    )
    assert decision.approved is False
    assert decision.reason == PolicyRejectionReason.PROBE_BUDGET_EXCEEDED


def test_c09_probe_mode_valid_approved() -> None:
    """Valid bounded probe mode with gas <= budget <= 1.0 is approved."""
    policy = ExecutionPolicy(
        mode="bounded_probe",
        probe_loss_budget_usd=Decimal("0.50"),
        probe_mode_enabled=True,
    )
    decision = evaluate_execution_policy(
        amount_in=100_000_000_000_000_000,
        expected_out=100_500_000_000_000_000,
        decimals=18,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        policy=policy,
    )
    assert decision.approved is True
    assert decision.policy.mode == PolicyMode.BOUNDED_PROBE


def test_c09_output_floor_mathematical_guarantee() -> None:
    """calculate_output_floor guarantees floor >= slippage_min and >= cost_floor."""
    amount_in = 100_000_000_000_000_000
    expected_out = 100_500_000_000_000_000
    slippage_bps = 50
    decimals = 18
    price = Decimal("2500.0")
    gas_usd = Decimal("0.25")

    floor = calculate_output_floor(
        amount_in=amount_in,
        expected_out=expected_out,
        slippage_bps=slippage_bps,
        decimals=decimals,
        base_asset_usd_price=price,
        conservative_gas_usd=gas_usd,
    )

    slippage_min = (expected_out * (10000 - slippage_bps)) // 10000
    scale = Decimal(10**decimals)
    gas_atoms = int(((gas_usd * scale) / price).to_integral_value(rounding=ROUND_CEILING))
    cost_floor = amount_in + gas_atoms + 1

    assert floor == max(slippage_min, cost_floor)
    assert floor <= expected_out


def test_c09_policy_serialization_roundtrip() -> None:
    """ExecutionPolicy serializes to dict and canonical JSON with zero loss."""
    original = ExecutionPolicy(
        mode=PolicyMode.BOUNDED_PROBE,
        max_amount_usd=Decimal("350.0"),
        slippage_bps=120,
        probe_loss_budget_usd=Decimal("0.75"),
        probe_mode_enabled=True,
    )
    data = original.to_dict()
    restored = ExecutionPolicy.from_dict(data)
    assert restored == original

    json_str = original.to_json()
    from_json_restored = ExecutionPolicy.from_json(json_str)
    assert from_json_restored == original


# ==============================================================================
# Planning Assembly Tests (atomic_execution/planning.py)
# ==============================================================================


def test_planning_build_execution_plan_from_validated_candidate() -> None:
    """ExecutionPlan assembles cleanly from ValidatedCandidate with verified invariants."""
    route, quote, state = _make_valid_2hop_bundle()
    candidate = ValidatedCandidate(
        route_ref=route,
        quote_evidence=quote,
        state_version=state,
        base_asset=route.base_asset,
        amount_in=quote.amount_in,
    )

    plan = build_execution_plan(
        candidate=candidate,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        current_time=1_700_000_000,
    )

    assert isinstance(plan, ExecutionPlan)
    assert plan.route_id == route.route_id
    assert plan.chain_id == ROBINHOOD_CHAIN_ID
    assert plan.quoter_block == state.block_number
    assert plan.deadline == 1_700_000_000 + DEFAULT_DEADLINE_SECONDS
    assert plan.target_router == CANONICAL_UNIVERSAL_ROUTER
    assert plan.min_amount_out.atoms > 0
    assert plan.min_amount_out.atoms <= quote.amount_out.atoms  # type: ignore[union-attr]
    assert plan.output_floor.atoms == plan.min_amount_out.atoms
    assert plan.net_atoms > 0
    assert plan.net_profit_usd > Decimal("0")


def test_planning_build_execution_plan_from_candidate_opportunity() -> None:
    """ExecutionPlan assembles cleanly from CandidateOpportunity envelope."""
    route, quote, state = _make_valid_2hop_bundle()
    opportunity = CandidateOpportunity(
        route_ref=route,
        quote_evidence=quote,
        state_version=state,
        opportunity_id="opp:test-123",
    )

    plan = build_execution_plan(
        candidate=opportunity,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        current_time=1_700_000_000,
    )

    assert plan.candidate_id == "opp:test-123"
    assert plan.route_id == route.route_id


def test_planning_build_execution_plan_roundtrip_dict_and_json() -> None:
    """ExecutionPlan serializes to dict and canonical JSON with 100% roundtrip fidelity."""
    route, quote, state = _make_valid_2hop_bundle()
    candidate = ValidatedCandidate(
        route_ref=route,
        quote_evidence=quote,
        state_version=state,
        base_asset=route.base_asset,
        amount_in=quote.amount_in,
    )

    original = build_execution_plan(
        candidate=candidate,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
        current_time=1_700_000_000,
    )

    data = original.to_dict()
    restored = ExecutionPlan.from_dict(data)

    assert restored.plan_id == original.plan_id
    assert restored.route_id == original.route_id
    assert restored.amount_in.atoms == original.amount_in.atoms
    assert restored.min_amount_out.atoms == original.min_amount_out.atoms
    assert restored.target_router == original.target_router
    assert restored.estimated_gas_usd == original.estimated_gas_usd
    assert restored.net_atoms == original.net_atoms

    json_str = original.to_json()
    from_json_restored = ExecutionPlan.from_json(json_str)
    assert from_json_restored.plan_id == original.plan_id
    assert from_json_restored.min_amount_out.atoms == original.min_amount_out.atoms


def test_planning_build_execution_plan_unquoted_fails() -> None:
    """Non-QUOTED quote evidence fails plan assembly."""
    route, revert_quote, state = _make_valid_2hop_bundle(status=QuoteStatus.CONTRACT_REVERT)
    candidate = CandidateOpportunity(
        route_ref=route,
        quote_evidence=revert_quote,
        state_version=state,
    )

    with pytest.raises(PolicyViolationError):
        build_execution_plan(
            candidate=candidate,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=Decimal("0.25"),
        )


def test_planning_build_execution_plan_excessive_amount_raises() -> None:
    """Building plan with trade size > 500 USD raises ExcessiveAmountError."""
    route, quote, state = _make_valid_2hop_bundle(amount_in_atoms=400_000_000_000_000_000)
    candidate = ValidatedCandidate(
        route_ref=route,
        quote_evidence=quote,
        state_version=state,
        base_asset=route.base_asset,
        amount_in=quote.amount_in,
    )

    with pytest.raises(ExcessiveAmountError):
        build_execution_plan(
            candidate=candidate,
            base_asset_usd_price=Decimal("2500.0"),
            conservative_gas_usd=Decimal("0.25"),
        )


def test_planning_evaluate_assembly_non_throwing() -> None:
    """evaluate_plan_assembly returns PlanAssemblyResult without throwing on rejection."""
    route, quote, state = _make_valid_2hop_bundle(
        amount_in_atoms=100_000_000_000_000_000,
        amount_out_atoms=100_000_000_000_000_000,  # net loss
    )
    candidate = ValidatedCandidate(
        route_ref=route,
        quote_evidence=quote,
        state_version=state,
        base_asset=route.base_asset,
        amount_in=quote.amount_in,
    )

    result = evaluate_plan_assembly(
        candidate=candidate,
        base_asset_usd_price=Decimal("2500.0"),
        conservative_gas_usd=Decimal("0.25"),
    )
    assert result.approved is False
    assert result.plan is None
    assert result.rejection_reason in (PolicyRejectionReason.UNPROFITABLE, PolicyRejectionReason.OUTPUT_FLOOR_NOT_MET)


# ==============================================================================
# Architecture and Static Boundary AST Checks
# ==============================================================================


def test_architecture_zero_float_in_planning_and_policy() -> None:
    """Static AST check: planning.py and policy.py must contain 0 float literals or calls."""
    root = Path(__file__).resolve().parents[2]
    target_files = [
        root / "atomic_execution" / "policy.py",
        root / "atomic_execution" / "planning.py",
    ]

    for fpath in target_files:
        tree = ast.parse(fpath.read_text(encoding="utf-8"), filename=str(fpath))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                pytest.fail(f"Float literal {node.value} found in {fpath.name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
                pytest.fail(f"float() call found in {fpath.name}")


def test_architecture_no_forbidden_imports() -> None:
    """Static AST check: planning.py and policy.py must not import legacy packages."""
    root = Path(__file__).resolve().parents[2]
    target_files = [
        root / "atomic_execution" / "policy.py",
        root / "atomic_execution" / "planning.py",
    ]
    forbidden_roots = {"arbitrage", "execution", "core", "chains", "backtest", "monitors"}

    for fpath in target_files:
        tree = ast.parse(fpath.read_text(encoding="utf-8"), filename=str(fpath))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.append(node.module)

            for mod in imported:
                root_pkg = mod.split(".")[0]
                assert root_pkg not in forbidden_roots, f"Forbidden import '{mod}' in {fpath.name}"
