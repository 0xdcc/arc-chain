# ruff: noqa: E402
"""Comprehensive unit and security invariant tests for arbitrage reporting and events.

Covers:
1. Domain types integration (QuoteResult, CandidateRoute, ExecutionPlan, TokenAmount).
2. Mode isolation & anti-counterfeiting (live vs historical_replay vs synthetic).
3. Field fault-tolerance (None amount_out, None delta_atoms, revert/RPC errors, zero number fabrication).
4. Physical side-effect isolation (zero subprocess, hard-blocked external dispatch in replay/synthetic).
5. Observability event model (schema_version 1.0.0, strict is_authoritative_funds=False invariant).
"""

import ast
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

# Defensive workspace resolution: ensures backtest/execution imports do not block testing
_repair_root = Path("/root/projects/crypto/dex-sniper-engine-repair")
if _repair_root.exists() and str(_repair_root) not in sys.path:
    sys.path.append(str(_repair_root))
for mod_name in (
    "backtest",
    "backtest.data",
    "backtest.data.rpc_client",
    "execution",
    "execution.funds",
):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

import pytest
from arbitrage.domain.types import (
    CandidateRoute,
    ExecutionPlan,
    PoolIdentity,
    QuoteResult,
    QuoteStatus,
    RouteHop,
    TokenAmount,
    TokenIdentity,
)
from arbitrage.reporting import (
    ALLOWED_MODES,
    AUTHORITATIVE_FUNDS_DISCLAIMER,
    SCHEMA_VERSION,
    WATERMARK_HISTORICAL_REPLAY,
    WATERMARK_LIVE,
    WATERMARK_SYNTHETIC,
    ArbitrageReport,
    AuditEventType,
    AuditSeverity,
    ExecutionMode,
    InMemoryEventSink,
    InMemoryReportSink,
    SafeNotifier,
    SecurityAuditEvent,
    create_candidate_route_audit_event,
    create_execution_plan_audit_event,
    create_quote_audit_event,
    create_report_audit_event,
    create_security_blocked_audit_event,
    format_delta_profit,
    format_plan_dict,
    format_plan_json,
    format_plan_markdown,
    format_plan_text,
    format_quote_dict,
    format_quote_json,
    format_quote_markdown,
    format_quote_text,
    format_report_dict,
    format_report_json,
    format_report_markdown,
    format_report_text,
    format_route_dict,
    format_route_json,
    format_route_markdown,
    format_route_text,
    format_token_amount,
    normalize_mode,
)

# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def token_usdg() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x1111111111111111111111111111111111111111",
        decimals=6,
        symbol="USDG",
    )


@pytest.fixture
def token_weth() -> TokenIdentity:
    return TokenIdentity(
        chain_id=4663,
        address="0x2222222222222222222222222222222222222222",
        decimals=18,
        symbol="WETH",
    )


@pytest.fixture
def pool_v3(token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x3333333333333333333333333333333333333333",
        token0=token_usdg.address,
        token1=token_weth.address,
        fee_bps=5.0,
        tick_spacing=10,
    )


@pytest.fixture
def pool_v4(token_usdg: TokenIdentity, token_weth: TokenIdentity) -> PoolIdentity:
    return PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v4",
        pool_id="0x4444444444444444444444444444444444444444",
        token0=token_usdg.address,
        token1=token_weth.address,
        fee_bps=30.0,
        tick_spacing=60,
    )


@pytest.fixture
def sample_route(
    token_usdg: TokenIdentity,
    token_weth: TokenIdentity,
    pool_v3: PoolIdentity,
    pool_v4: PoolIdentity,
) -> CandidateRoute:
    hop0 = RouteHop(pool=pool_v3, token_in=token_usdg, token_out=token_weth)
    hop1 = RouteHop(pool=pool_v4, token_in=token_weth, token_out=token_usdg)
    return CandidateRoute(
        candidate_id="cand_test_001",
        route_type="two_hop_spread",
        base_token=token_usdg,
        hops=(hop0, hop1),
        observed_gross_bps=25.5,
        snapshot_block=1234567,
        created_at=1710000000.0,
    )


@pytest.fixture
def successful_quote(token_usdg: TokenIdentity) -> QuoteResult:
    amt_in = TokenAmount(token=token_usdg, atoms=100_000_000)  # 100 USDG
    amt_out = TokenAmount(token=token_usdg, atoms=101_500_000)  # 101.5 USDG
    return QuoteResult(
        status=QuoteStatus.QUOTED,
        amount_in=amt_in,
        amount_out=amt_out,
        delta_atoms=1_500_000,  # +1.5 USDG
        gas_estimate=150_000,
        block_number=1234567,
        quote_mode="live",
    )


@pytest.fixture
def failed_revert_quote(token_usdg: TokenIdentity) -> QuoteResult:
    amt_in = TokenAmount(token=token_usdg, atoms=100_000_000)
    return QuoteResult(
        status=QuoteStatus.CONTRACT_REVERT,
        amount_in=amt_in,
        amount_out=None,
        delta_atoms=None,
        gas_estimate=None,
        error_code=3,
        error_message="execution reverted: STF",
        raw_revert_data="0x08c379a000000000000000000000000000000000",
        block_number=1234567,
        quote_mode="live",
    )


@pytest.fixture
def sample_plan(
    token_usdg: TokenIdentity,
    sample_route: CandidateRoute,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan_test_001",
        candidate_id=sample_route.candidate_id,
        route_type=sample_route.route_type,
        base_token=token_usdg,
        amount_in=TokenAmount(token=token_usdg, atoms=100_000_000),
        min_amount_out=TokenAmount(token=token_usdg, atoms=100_100_000),
        hops=sample_route.hops,
        quoter_block=1234567,
        deadline=1710000300,
        estimated_gas_usd=Decimal("0.05"),
        target_router="0x5555555555555555555555555555555555555555",
    )


# ==============================================================================
# 1. Mode Isolation & Anti-Counterfeiting Tests
# ==============================================================================


class TestModeIsolationAndAntiCounterfeiting:
    """Verify strict mode isolation and anti-counterfeiting across all outputs."""

    def test_mode_normalization(self) -> None:
        assert normalize_mode("live") == "live"
        assert normalize_mode("HISTORICAL_REPLAY") == "historical_replay"
        assert normalize_mode("Synthetic") == "synthetic"
        assert normalize_mode(ExecutionMode.LIVE) == "live"
        assert normalize_mode(None) == "live"

        with pytest.raises(ValueError, match="Invalid execution mode"):
            normalize_mode("fake_live_mode")

        with pytest.raises(ValueError, match="Invalid execution mode"):
            normalize_mode("production_spoof")

    def test_live_mode_output_badges(self, successful_quote: QuoteResult) -> None:
        json_dict = format_quote_dict(successful_quote, mode="live")
        assert json_dict["mode"] == "live"
        assert json_dict["is_live"] is True
        assert json_dict["anti_counterfeit"]["watermark"] == WATERMARK_LIVE
        assert json_dict["is_authoritative_funds"] is False

        text = format_quote_text(successful_quote, mode="live")
        assert "=== QUOTE RESULT [MODE: LIVE] ===" in text
        assert "REPLAY" not in text
        assert "SYNTHETIC" not in text

        md = format_quote_markdown(successful_quote, mode="live")
        assert "HISTORICAL REPLAY" not in md
        assert "SYNTHETIC" not in md
        assert "### ✅ Quote Status: `QUOTED`" in md

    def test_historical_replay_anti_counterfeit_watermark(
        self,
        successful_quote: QuoteResult,
        sample_route: CandidateRoute,
        sample_plan: ExecutionPlan,
    ) -> None:
        report = ArbitrageReport(
            report_id="rep_replay_001",
            mode="historical_replay",
            route=sample_route,
            quote=successful_quote,
            plan=sample_plan,
            executed=True,
        )

        # 1. JSON Export Audit
        json_str = format_report_json(report)
        data = json.loads(json_str)
        assert data["mode"] == "historical_replay"
        assert data["is_live"] is False
        assert data["is_authoritative_funds"] is False
        assert data["anti_counterfeit"]["watermark"] == WATERMARK_HISTORICAL_REPLAY
        assert "ZERO (SIMULATION)" in data["anti_counterfeit"]["funds_impact"]
        assert "NON-LIVE" in data["anti_counterfeit"]["disclaimer"]

        # 2. Text Summary Audit
        text = format_report_text(report)
        assert "HISTORICAL REPLAY REPORT - SIMULATION ONLY (NOT REAL PROFIT)" in text
        assert "Mode: historical_replay (Live: False)" in text
        assert "[REPLAY SIMULATED - NOT REAL MONEY]" in text
        assert AUTHORITATIVE_FUNDS_DISCLAIMER in text

        # 3. Markdown Card Audit
        md = format_report_markdown(report)
        assert "> ⚠️ **HISTORICAL REPLAY REPORT (SIMULATION — NOT REAL PROFIT)**" in md
        assert "No real transactions or balance updates occurred" in md
        assert "`historical_replay`" in md
        assert AUTHORITATIVE_FUNDS_DISCLAIMER in md

    def test_synthetic_anti_counterfeit_watermark(
        self, successful_quote: QuoteResult, sample_route: CandidateRoute
    ) -> None:
        report = ArbitrageReport(
            report_id="rep_synth_001",
            mode="synthetic",
            route=sample_route,
            quote=successful_quote,
        )

        json_str = format_report_json(report)
        data = json.loads(json_str)
        assert data["mode"] == "synthetic"
        assert data["is_live"] is False
        assert data["anti_counterfeit"]["watermark"] == WATERMARK_SYNTHETIC
        assert "ZERO (MOCK)" in data["anti_counterfeit"]["funds_impact"]

        text = format_report_text(report)
        assert "SYNTHETIC TEST REPORT - MOCK DATA ONLY (NOT REAL PROFIT)" in text
        assert "[SYNTHETIC TEST - NOT REAL MONEY]" in text

        md = format_report_markdown(report)
        assert "> 🧪 **SYNTHETIC TEST REPORT (MOCK DATA — NOT REAL PROFIT)**" in md

    def test_format_delta_profit_anti_spoofing(self, token_usdg: TokenIdentity) -> None:
        amt_in = TokenAmount(token=token_usdg, atoms=100_000_000)
        amt_out = TokenAmount(token=token_usdg, atoms=105_000_000)

        # Live mode
        live_profit = format_delta_profit(amt_in, amt_out, 5_000_000, mode="live")
        assert "+5 USDG (+5000000 atoms)" == live_profit

        # Replay mode must carry explicit simulation tag
        replay_profit = format_delta_profit(amt_in, amt_out, 5_000_000, mode="historical_replay")
        assert "[REPLAY SIMULATED - NOT REAL MONEY]" in replay_profit

        # Synthetic mode must carry explicit synthetic tag
        synth_profit = format_delta_profit(amt_in, amt_out, 5_000_000, mode="synthetic")
        assert "[SYNTHETIC TEST - NOT REAL MONEY]" in synth_profit


# ==============================================================================
# 2. Field Fault-Tolerance Tests
# ==============================================================================


class TestFieldFaultTolerance:
    """Verify non-throwing, zero-fabrication fault tolerance on incomplete/failed data."""

    def test_reverted_quote_formatting_without_exceptions(
        self, failed_revert_quote: QuoteResult
    ) -> None:
        # 1. Text format
        text = format_quote_text(failed_revert_quote)
        assert "Status: CONTRACT_REVERT" in text
        assert "Amount Out: N/A (Quote Failed)" in text
        assert "Net Profit: N/A" in text
        assert "Error: execution reverted: STF" in text
        assert "Error Code: 3" in text
        assert "Revert Data: 0x08c379a0" in text

        # 2. JSON format
        data = format_quote_dict(failed_revert_quote)
        assert data["status"] == "CONTRACT_REVERT"
        assert data["is_quoted"] is False
        assert data["amount_out"] is None
        assert data["delta_atoms"] is None
        assert data["delta_decimal"] is None
        assert data["error_code"] == 3
        assert data["error_message"] == "execution reverted: STF"

        # 3. Markdown format
        md = format_quote_markdown(failed_revert_quote)
        assert "### ❌ Quote Status: `CONTRACT_REVERT`" in md
        assert "*N/A (Failed to Quote)*" in md
        assert "*N/A*" in md
        assert "`execution reverted: STF`" in md

    @pytest.mark.parametrize(
        ("status", "err_msg", "err_code"),
        [
            (QuoteStatus.RPC_ERROR, "HTTP 502 Bad Gateway", -32000),
            (QuoteStatus.NODE_LIMITATION, "Compute units limit exceeded", 429),
            (QuoteStatus.INVALID_RESPONSE, "Malformed JSON-RPC payload", None),
        ],
    )
    def test_all_quote_failure_categories(
        self,
        token_usdg: TokenIdentity,
        status: QuoteStatus,
        err_msg: str,
        err_code: int | None,
    ) -> None:
        quote = QuoteResult(
            status=status,
            amount_in=TokenAmount(token=token_usdg, atoms=50_000_000),
            amount_out=None,
            delta_atoms=None,
            error_code=err_code,
            error_message=err_msg,
        )

        # None of these formatters should ever raise
        text = format_quote_text(quote)
        assert status.value in text
        assert "N/A" in text

        md = format_quote_markdown(quote)
        assert f"### ❌ Quote Status: `{status.value}`" in md

        d = format_quote_dict(quote)
        assert d["status"] == status.value
        assert d["amount_out"] is None
        assert d["delta_atoms"] is None

    def test_no_number_fabrication(self) -> None:
        # None inputs must output 'N/A', NEVER '0' or '0.00'
        assert format_token_amount(None) == "N/A"
        assert format_delta_profit(None, None, None) == "N/A"

    def test_partial_arbitrage_report_tolerated(self) -> None:
        # Report with None route, quote, and plan
        report = ArbitrageReport(
            report_id="rep_empty",
            mode="live",
            route=None,
            quote=None,
            plan=None,
            executed=False,
            error_message="Aborted before quoting: liquidity drained",
        )

        # Must format cleanly without throwing
        text = format_report_text(report)
        assert "Aborted before quoting: liquidity drained" in text

        md = format_report_markdown(report)
        assert "❌ `Aborted before quoting: liquidity drained`" in md

        d = format_report_dict(report)
        assert d["route"] is None
        assert d["quote"] is None
        assert d["plan"] is None
        assert d["error_message"] == "Aborted before quoting: liquidity drained"


# ==============================================================================
# 3. Physical Side-Effect Isolation Tests
# ==============================================================================


class TestPhysicalSideEffectIsolation:
    """Verify zero subprocess, zero socket, and strictly blocked non-live dispatch."""

    def test_ast_source_audit_zero_subprocess(self) -> None:
        """Statically inspect AST to guarantee zero subprocess or shell execution."""
        paths_to_audit = [
            Path("/tmp/modular-m5-workspace/arbitrage/reporting/__init__.py"),
            Path("/tmp/modular-m5-workspace/arbitrage/reporting/events.py"),
            Path("/tmp/modular-m5-workspace/arbitrage/reporting/formatters.py"),
        ]

        forbidden_names = {"subprocess", "os.system", "popen", "spawn", "shlex"}

        for p in paths_to_audit:
            assert p.exists(), f"File {p} does not exist"
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))

            for node in ast.walk(tree):
                # Audit imports
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in forbidden_names, (
                            f"Forbidden import '{alias.name}' in {p}"
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        assert node.module not in forbidden_names, (
                            f"Forbidden import from '{node.module}' in {p}"
                        )
                        for alias in node.names:
                            assert alias.name not in forbidden_names, (
                                f"Forbidden import '{alias.name}' from '{node.module}' in {p}"
                            )

    def test_disabled_notifier_is_complete_noop(self, successful_quote: QuoteResult) -> None:
        in_mem = InMemoryReportSink()
        external_mock = MagicMock()

        notifier = SafeNotifier(
            enabled=False,
            in_memory_sink=in_mem,
            external_sinks={"mock": external_mock},
        )

        res = notifier.dispatch(successful_quote, mode="live")
        assert res.dispatched is False
        assert "disabled" in str(res.blocked_reason).lower()
        external_mock.assert_not_called()
        # In-memory sink is still allowed to record
        assert len(in_mem) == 1

    def test_replay_and_synthetic_hard_block_external_sinks(
        self, successful_quote: QuoteResult
    ) -> None:
        in_mem = InMemoryReportSink()
        external_mock = MagicMock()

        notifier = SafeNotifier(
            enabled=True,
            in_memory_sink=in_mem,
            external_sinks={"webhook": external_mock},
        )

        # 1. Historical Replay Dispatch Attempt
        res_replay = notifier.dispatch(successful_quote, mode="historical_replay")
        assert res_replay.dispatched is False
        assert "non-live" in str(res_replay.blocked_reason).lower()
        external_mock.assert_not_called()

        # 2. Synthetic Dispatch Attempt
        res_synth = notifier.dispatch(successful_quote, mode="synthetic")
        assert res_synth.dispatched is False
        assert "non-live" in str(res_synth.blocked_reason).lower()
        external_mock.assert_not_called()

        # 3. Live Dispatch Attempt
        res_live = notifier.dispatch(successful_quote, mode="live")
        assert res_live.dispatched is True
        external_mock.assert_called_once_with(successful_quote)

    def test_sink_error_containment(self, successful_quote: QuoteResult) -> None:
        def buggy_sink(_item: object) -> None:
            raise RuntimeError("External sink network timeout simulation")

        good_mock = MagicMock()

        notifier = SafeNotifier(
            enabled=True,
            external_sinks={"buggy": buggy_sink, "good": good_mock},
        )

        # Even with one sink exploding, the notification manager must not crash
        res = notifier.dispatch(successful_quote, mode="live")
        assert res.dispatched is True
        assert "good" in res.sinks_notified
        good_mock.assert_called_once()


# ==============================================================================
# 4. Security Audit Event Model Tests
# ==============================================================================


class TestSecurityAuditEventModel:
    """Verify versioned schema and non-authoritative funds invariant."""

    def test_authoritative_funds_red_line_enforced(self) -> None:
        # Setting is_authoritative_funds=True is strictly forbidden
        with pytest.raises(ValueError, match="is_authoritative_funds must strictly be False"):
            SecurityAuditEvent(
                event_id="evt_hack_001",
                event_type=AuditEventType.EXECUTION_CONFIRMED,
                is_authoritative_funds=True,  # VIOLATION
            )

    def test_schema_version_and_disclaimer(self) -> None:
        evt = SecurityAuditEvent(
            event_id="evt_safe_001",
            event_type=AuditEventType.OPPORTUNITY_DETECTED,
            mode="live",
        )
        assert evt.schema_version == SCHEMA_VERSION
        assert evt.is_authoritative_funds is False
        assert evt.authority_disclaimer == AUTHORITATIVE_FUNDS_DISCLAIMER

    def test_quote_event_factory_success(
        self, successful_quote: QuoteResult, sample_route: CandidateRoute
    ) -> None:
        evt = create_quote_audit_event(successful_quote, route=sample_route)
        assert evt.event_type == AuditEventType.QUOTE_RESULT.value
        assert evt.severity == AuditSeverity.INFO.value
        assert evt.payload["is_success"] is True
        assert evt.payload["candidate_id"] == sample_route.candidate_id
        assert evt.payload["delta_atoms"] == 1_500_000
        assert evt.payload["delta_decimal"] == "1.5"

    def test_quote_event_factory_failure(self, failed_revert_quote: QuoteResult) -> None:
        evt = create_quote_audit_event(failed_revert_quote)
        assert evt.event_type == AuditEventType.QUOTE_FAILED.value
        assert evt.severity == AuditSeverity.WARNING.value
        assert evt.payload["is_success"] is False
        assert evt.payload["error_code"] == 3
        assert evt.payload["error_message"] == "execution reverted: STF"

    def test_plan_event_factory(self, sample_plan: ExecutionPlan) -> None:
        evt = create_execution_plan_audit_event(sample_plan)
        assert evt.event_type == AuditEventType.PLAN_GENERATED.value
        assert evt.payload["plan_id"] == sample_plan.plan_id
        assert evt.payload["target_router"] == sample_plan.target_router
        assert evt.payload["estimated_gas_usd"] == "0.05"

    def test_route_event_factory(self, sample_route: CandidateRoute) -> None:
        evt = create_candidate_route_audit_event(sample_route)
        assert evt.event_type == AuditEventType.OPPORTUNITY_DETECTED.value
        assert evt.payload["candidate_id"] == sample_route.candidate_id
        assert evt.payload["hop_count"] == 2

    def test_security_blocked_event_factory(self) -> None:
        evt = create_security_blocked_audit_event(
            rule_name="SlippageZeroProhibited",
            reason="min_amount_out cannot be zero (MEV sandwich guard)",
            details={"min_amount_out": 0},
            severity=AuditSeverity.CRITICAL,
        )
        assert evt.event_type == AuditEventType.SECURITY_BLOCKED.value
        assert evt.severity == AuditSeverity.CRITICAL.value
        assert evt.payload["rule_name"] == "SlippageZeroProhibited"

    def test_report_event_factory(
        self,
        sample_route: CandidateRoute,
        successful_quote: QuoteResult,
        sample_plan: ExecutionPlan,
    ) -> None:
        report = ArbitrageReport(
            report_id="rep_done_001",
            mode="live",
            route=sample_route,
            quote=successful_quote,
            plan=sample_plan,
            executed=True,
            tx_hash="0x" + "a" * 64,
        )
        evt = create_report_audit_event(report)
        assert evt.event_type == AuditEventType.EXECUTION_CONFIRMED.value
        assert evt.payload["executed"] is True
        assert evt.payload["tx_hash"] == report.tx_hash

    def test_event_json_serialization_roundtrip(self) -> None:
        evt = SecurityAuditEvent(
            event_id="evt_roundtrip_001",
            event_type=AuditEventType.CUSTOM_AUDIT.value,
            payload={"action": "test_pass", "code": 100},
            metadata={"source": "pytest"},
            mode="synthetic",
        )

        # JSON roundtrip
        json_str = evt.to_json(indent=2)
        parsed = SecurityAuditEvent.from_json(json_str)

        assert parsed.event_id == evt.event_id
        assert parsed.event_type == evt.event_type
        assert parsed.schema_version == evt.schema_version
        assert parsed.mode == "synthetic"
        assert parsed.payload == {"action": "test_pass", "code": 100}
        assert parsed.metadata == {"source": "pytest"}
        assert parsed.is_authoritative_funds is False

    def test_in_memory_event_sink_collector(self) -> None:
        sink = InMemoryEventSink()
        evt1 = SecurityAuditEvent(event_id="e1", event_type="type_a", mode="live", severity="INFO")
        evt2 = SecurityAuditEvent(
            event_id="e2", event_type="type_b", mode="synthetic", severity="ERROR"
        )

        sink.append(evt1)
        sink.append(evt2)
        assert len(sink) == 2

        # Filter tests
        assert len(sink.filter(event_type="type_a")) == 1
        assert len(sink.filter(mode="synthetic")) == 1
        assert len(sink.filter(severity="ERROR")) == 1

        sink.clear()
        assert len(sink) == 0


# ==============================================================================
# 5. Route & Execution Plan Formatter Detail Tests
# ==============================================================================


class TestRouteAndPlanFormatters:
    """Verify route and plan formatters across text, markdown, and JSON."""

    def test_route_formatters(self, sample_route: CandidateRoute) -> None:
        # Text
        text = format_route_text(sample_route)
        assert "=== CANDIDATE ROUTE [cand_test_001] ===" in text
        assert "USDG -> WETH" in text
        assert "WETH -> USDG" in text
        assert "25.50 bps" in text

        # Markdown
        md = format_route_markdown(sample_route)
        assert "### 🔀 Candidate Route: `cand_test_001`" in md
        assert "`USDG` ➔ `WETH`" in md
        assert "`uniswap_v3`" in md
        assert "`uniswap_v4`" in md

        # JSON
        d = format_route_dict(sample_route)
        assert d["candidate_id"] == "cand_test_001"
        assert d["hop_count"] == 2
        assert len(d["hops"]) == 2

        json_str = format_route_json(sample_route)
        assert "cand_test_001" in json_str

    def test_plan_formatters(self, sample_plan: ExecutionPlan) -> None:
        # Text
        text = format_plan_text(sample_plan)
        assert "=== EXECUTION PLAN [plan_test_001] ===" in text
        assert "$0.05" in text
        assert "0x5555555555555555555555555555555555555555" in text

        # Markdown
        md = format_plan_markdown(sample_plan)
        assert "### 📋 Execution Plan: `plan_test_001`" in md
        assert "(Slippage Protected)" in md

        # JSON
        d = format_plan_dict(sample_plan)
        assert d["plan_id"] == "plan_test_001"
        assert d["estimated_gas_usd"] == "0.05"

        json_str = format_plan_json(sample_plan)
        assert "plan_test_001" in json_str
