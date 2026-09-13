"""Arc Shadow Evaluation Runtime (T38)

Assembles the end-to-end readonly shadow pipeline:
- Discrete Quoting via ArcQuoteBridge
- Economic Cost Breakdown via ArcEconomicEvaluator
- 4-Tier Shadow Evaluation via ArcShadowEvaluationService
- Hash-chained Opportunity Ledger persistence via ArcOpportunityLedger
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from arbitrage_contracts.arc_extensions import (
    SimulationEvidenceBridge,
    SimulationStatus,
)
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.quote import DataMode, HopRef, RouteRef
from arbitrage_contracts.state import StateVersion
from arc_opportunities.costs import create_gas_cost_evidence
from arc_opportunities.economics import USDC_SHARED_BALANCE_DOMAIN, ArcEconomicEvaluator
from arc_opportunities.ledger import ArcOpportunityLedger
from arc_opportunities.quote_bridge import ArcQuoteBridge
from arc_opportunities.shadow import ArcShadowEvaluationService, ShadowEvaluationResult
from state_graph.clmm_math import get_sqrt_ratio_at_tick
from state_graph.types import FrozenEpoch, PoolStateSnapshot


@dataclass(frozen=True)
class ShadowPipelineConfig:
    """Configuration for arc_shadow execution."""

    chain_id: int
    ledger_dir: Path
    catalog_path: Path | None = None
    max_amount_usd: float = 500.0
    fixture_mode: bool = False
    rpc_endpoint: str | None = None

    def __post_init__(self) -> None:
        if self.chain_id not in (5042, 5042002):
            raise ValueError(f"Invalid chain_id: {self.chain_id}")
        if self.max_amount_usd <= 0 or self.max_amount_usd > 500.0:
            raise ValueError("max_amount_usd must be in (0, 500.0]")


def _reject_path_symlinks(path: Path) -> None:
    """Ensure neither the target path nor any ancestor directory is a symlink."""
    abs_norm = (Path.cwd() / path) if not path.is_absolute() else path
    abs_norm = Path(os.path.normpath(str(abs_norm)))
    for part in [abs_norm] + list(abs_norm.parents):
        if part.is_symlink() or os.path.islink(part):
            raise PermissionError(f"Symlink detected in path component: {part}")


@contextmanager
def _locked_output_dir(dir_path: Path):
    """Acquire an exclusive process lock on an output directory."""
    _reject_path_symlinks(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    lock_file = dir_path / ".lock"
    _reject_path_symlinks(lock_file)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def run_shadow_pipeline(config: ShadowPipelineConfig) -> dict[str, Any]:
    """Execute complete shadow evaluation pipeline."""
    start_time = time.monotonic()

    if not config.fixture_mode:
        # Reject before mkdir()/ledger construction so an unauthorized live
        # invocation is observationally read-only on the local filesystem.
        raise PermissionError(
            "Live shadow evaluation requires verified RPC endpoint authorization (G2_LIVE)."
        )

    _reject_path_symlinks(config.ledger_dir)

    with _locked_output_dir(config.ledger_dir):
        ledger_file = config.ledger_dir / "opportunities.jsonl"
        checkpoint_file = Path(str(ledger_file) + ".checkpoint")
        protected_outputs = (ledger_file, checkpoint_file)

        for p in protected_outputs:
            if p.is_symlink() or os.path.islink(p):
                raise FileExistsError(f"Refusing to overwrite existing shadow evidence: {p}")

        collisions = [str(path) for path in protected_outputs if os.path.lexists(path)]
        if collisions:
            raise FileExistsError(
                "Refusing to overwrite existing shadow evidence: " + ", ".join(collisions)
            )

        ledger = ArcOpportunityLedger(ledger_file)

        quote_bridge = ArcQuoteBridge()
        economic_evaluator = ArcEconomicEvaluator()
        service = ArcShadowEvaluationService(
            bridge=quote_bridge, evaluator=economic_evaluator, ledger=ledger
        )

        evaluated_results: list[ShadowEvaluationResult] = []

        if config.fixture_mode:
            # Deterministic 2-hop synthetic cycle
            usdc_token = TokenKey(config.chain_id, "0x3600000000000000000000000000000000000001")
            weth_token = TokenKey(config.chain_id, "0x4200000000000000000000000000000000000002")

            usdc_asset = AssetRef(
                interface_kind="erc20",
                chain_id=config.chain_id,
                token_key=usdc_token,
                balance_domain_id=USDC_SHARED_BALANCE_DOMAIN,
            )
            weth_asset = AssetRef(
                interface_kind="erc20",
                chain_id=config.chain_id,
                token_key=weth_token,
            )
            decimals_map = {usdc_asset: 6, weth_asset: 18}

            p1_key = PoolKey(
                config.chain_id,
                "uniswap_v3",
                "factory",
                "0x" + "11" * 20,
                "address",
                "0x" + "01".zfill(40),
            )
            p2_key = PoolKey(
                config.chain_id,
                "uniswap_v3",
                "factory",
                "0x" + "11" * 20,
                "address",
                "0x" + "02".zfill(40),
            )

            p1 = PoolDescriptor(
                key=p1_key,
                currency0=usdc_asset,
                currency1=weth_asset,
                fee_model=FeeModel.static(500),
                tick_spacing=10,
                deployment_status="deployed",
            )
            p2 = PoolDescriptor(
                key=p2_key,
                currency0=usdc_asset,
                currency1=weth_asset,
                fee_model=FeeModel.static(500),
                tick_spacing=10,
                deployment_status="deployed",
            )

            hop1 = HopRef(p1.key, usdc_asset, weth_asset, "zero_for_one", p1)
            hop2 = HopRef(p2.key, weth_asset, usdc_asset, "one_for_zero", p2)
            route = RouteRef(config.chain_id, usdc_asset, (hop1, hop2))

            sv = StateVersion(
                chain_id=config.chain_id,
                block_domain="l1",
                block_number=1000,
                block_hash="0x" + "aa" * 32,
                received_at_ms=1726000000000,
                complete_through_block=1000,
                completeness="ready",
                finality="safe",
            )

            # Profitable spread
            snap1 = PoolStateSnapshot(
                p1.key.pool_id,
                get_sqrt_ratio_at_tick(25),
                25,
                500_000_000_000_000_000,
                500,
                10,
                1000,
                "0x" + "aa" * 32,
            )
            snap2 = PoolStateSnapshot(
                p2.key.pool_id,
                get_sqrt_ratio_at_tick(-25),
                -25,
                500_000_000_000_000_000,
                500,
                10,
                1000,
                "0x" + "aa" * 32,
            )
            epoch = FrozenEpoch("ep-shadow-cli", sv, (snap1, snap2), 1726000000000)

            gas = create_gas_cost_evidence(cost_atoms=100, currency="USDC")
            sim_verified = SimulationEvidenceBridge(
                call_succeeded=True,
                output_verified=True,
                status=SimulationStatus.CALL_SUCCEEDED,
                net_output_atoms=10000,
                gas_used_atoms=100,
                backend="arc_readonly_mock",
            )

            res = service.evaluate_candidate(
                route=route,
                amount_in=Amount(usdc_asset, 1_000_000, 6),
                epoch=epoch,
                base_asset_usd_price=Decimal("1.0"),
                gas_evidence=gas,
                simulation_evidence=sim_verified,
                data_mode=DataMode.SYNTHETIC,
                token_decimals=decimals_map,
            )
            evaluated_results.append(res)

        elapsed = time.monotonic() - start_time
        total = len(evaluated_results)
        profitable = sum(
            1
            for r in evaluated_results
            if r.is_actionable or (r.breakdown is not None and r.breakdown.economic_status == "profitable")
        )

        return {
            "status": "SUCCESS",
            "chain_id": config.chain_id,
            "total_evaluated": total,
            "profitable_candidates": profitable,
            "ledger_file": str(ledger_file),
            "ledger_entries_count": ledger.confirmed_sequence,
            "elapsed_seconds": round(elapsed, 4),
            "is_fixture_mode": config.fixture_mode,
        }
