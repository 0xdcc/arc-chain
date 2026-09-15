"""Unified thin entrypoint and CLI assembly for dex-sniper-engine.

Provides a consolidated command-line interface with strict privilege isolation:
- monitor:  Assembles only read-only components (ReadOnlyMonitorService).
- quote:    Assembles Quoter and MarketSnapshot.
- replay:   Assembles ReplayAdapter to strictly consume 30 historical RPC records.
- simulate: Assembles ExecutionPlan and executes simulation via ExecutionService.simulate_plan.
- trade:    Enforces mandatory --dry-run safety guardrails; intercepts live broadcast attempts.

Privilege Isolation Guarantee:
- Subcommands 'monitor', 'quote', and 'replay' never import any private keys,
  wallet signatures, or transaction execution/broadcasting components.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class AuthorizationBlockedError(RuntimeError):
    """Raised when live trade execution or unauthorized dispatch is blocked by security guardrails."""


# -----------------------------------------------------------------------------
# Internal helpers for simulation/trade plan assembly (lazy execution context)
# -----------------------------------------------------------------------------


def _build_default_execution_plan(
    base: str = "WETH",
    amount_in: int = 10**16,
    plan_id: str | None = None,
    target_router: str | None = None,
) -> Any:
    """Construct an immutable cyclic ExecutionPlan for simulation and dry-run dispatch."""
    from arbitrage.domain.types import (
        ExecutionPlan,
        PoolIdentity,
        RouteHop,
        TokenAmount,
        TokenIdentity,
    )
    from execution.protocols import ROUTER

    if base == "WETH":
        base_tok = TokenIdentity(
            chain_id=4663,
            address="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            decimals=18,
            symbol="WETH",
        )
        alt_tok = TokenIdentity(
            chain_id=4663,
            address="0x5fc5360d0400a0fd4f2af552add042d716f1d168",
            decimals=6,
            symbol="USDG",
        )
    else:
        base_tok = TokenIdentity(
            chain_id=4663,
            address="0x5fc5360d0400a0fd4f2af552add042d716f1d168",
            decimals=6,
            symbol="USDG",
        )
        alt_tok = TokenIdentity(
            chain_id=4663,
            address="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            decimals=18,
            symbol="WETH",
        )

    pool1 = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x" + "33" * 20,
        token0=base_tok.address,
        token1=alt_tok.address,
        fee_bps=5.0,
        tick_spacing=10,
    )
    pool2 = PoolIdentity(
        chain_id=4663,
        protocol="uniswap_v3",
        pool_id="0x" + "44" * 20,
        token0=alt_tok.address,
        token1=base_tok.address,
        fee_bps=5.0,
        tick_spacing=10,
    )

    hop1 = RouteHop(pool=pool1, token_in=base_tok, token_out=alt_tok)
    hop2 = RouteHop(pool=pool2, token_in=alt_tok, token_out=base_tok)

    return ExecutionPlan(
        plan_id=plan_id or f"plan_{base.lower()}_{amount_in}",
        candidate_id="cand_cyclic_01",
        route_type="cyclic",
        base_token=base_tok,
        amount_in=TokenAmount(token=base_tok, atoms=amount_in),
        min_amount_out=TokenAmount(token=base_tok, atoms=max(1, amount_in - 1000)),
        hops=(hop1, hop2),
        quoter_block=58239319,
        deadline=1893456000,
        estimated_gas_usd=Decimal("0.05"),
        target_router=target_router or ROUTER,
    )


class _OfflineSimulationRpc:
    """Offline mock RPC caller for dry-run simulation in uncredentialed sandboxes."""

    def call(self, method: str, params: list[Any]) -> str:
        if method == "eth_call":
            return "0x"
        return "0x0"


def _resolve_rpc_client(rpc_arg: Any) -> Any:
    """Resolve an RPC argument into a usable RPC client."""
    if rpc_arg is None:
        return _OfflineSimulationRpc()
    if isinstance(rpc_arg, str) and rpc_arg.startswith(("http://", "https://")):
        from web3 import Web3

        return Web3(Web3.HTTPProvider(rpc_arg))
    return rpc_arg


# -----------------------------------------------------------------------------
# Subcommand Handlers (Strictly lazy-imported for physical isolation)
# -----------------------------------------------------------------------------


def cmd_monitor(args: argparse.Namespace) -> int:
    """Execute read-only market monitor service."""
    from apps.monitor.service import ReadOnlyMonitorService
    from research.market_data.catalog import get_verified_token, list_pools
    from research.market_data.pool_reader import SnapshotCoordinator

    rpc_client = getattr(args, "rpc", None)
    if not rpc_client:
        sys.stderr.write("[INPUT_INSUFFICIENT] Explicit --rpc is required; no live result was obtained.\n")
        return 1
    if isinstance(rpc_client, str) and rpc_client.startswith(("http://", "https://")):
        from web3 import Web3

        rpc_client = Web3(Web3.HTTPProvider(rpc_client))

    coordinator = SnapshotCoordinator(rpc=rpc_client)

    base_tok = None
    base_sym = getattr(args, "base_token", "USDG")
    if base_sym:
        try:
            base_tok = get_verified_token(base_sym)
        except Exception:
            base_tok = None

    service = ReadOnlyMonitorService(
        coordinator=coordinator,
        rpc=rpc_client,
        min_gross_bps=getattr(args, "min_gross_bps", 10.0),
        base_token=base_tok,
    )

    pools = list_pools()
    block = getattr(args, "block", None)
    result = service.poll_once(pools=pools, block_number=block)

    status = result.get("status", "unknown")
    block_num = result.get("block_number")
    cands = result.get("candidates", [])
    reports = result.get("reports", [])

    print(
        f"[MONITOR] Poll completed: status={status}, "
        f"block={block_num}, candidates={len(cands)}, reports={len(reports)}"
    )
    return 0 if status == "success" and type(block_num) is int and block_num > 0 else 1


def cmd_quote(args: argparse.Namespace) -> int:
    """Assemble Quoter and MarketSnapshot to query DEX quotes."""
    from research.market_data.catalog import list_pools
    from research.market_data.pool_reader import SnapshotCoordinator
    from research.quoting.quoter import (
        Quoter,
        build_canonical_route_a,
        build_canonical_route_b,
    )

    rpc_client = getattr(args, "rpc", None)
    if not rpc_client:
        sys.stderr.write("[INPUT_INSUFFICIENT] Explicit --rpc is required; no live result was obtained.\n")
        return 1
    if isinstance(rpc_client, str) and rpc_client.startswith(("http://", "https://")):
        from web3 import Web3

        rpc_client = Web3(Web3.HTTPProvider(rpc_client))

    coordinator = SnapshotCoordinator(rpc=rpc_client)
    snapshot = coordinator.read_market_snapshot(
        pools=list_pools(),
        rpc=rpc_client,
        block_number=getattr(args, "block", None),
    )

    quoter = Quoter(rpc_client=rpc_client)

    route_name = getattr(args, "route", "canonical_a")
    if route_name == "canonical_b":
        route = build_canonical_route_b()
    else:
        route = build_canonical_route_a()

    amount_in = getattr(args, "amount_in", 10**15)
    quote_res = quoter.quote_route(
        route=route,
        amount_in=amount_in,
        block_identifier=getattr(args, "block", None),
        quote_mode="live" if rpc_client else "synthetic",
    )

    out_atoms = quote_res.amount_out.atoms if quote_res.amount_out else None
    print(
        f"[QUOTE] snapshot_block={snapshot.block_number}, status={quote_res.status.value}, "
        f"in={quote_res.amount_in.atoms} {quote_res.amount_in.token.symbol}, "
        f"out={out_atoms}, hops={len(route.hops)}"
    )
    return 0 if quote_res.status.value == "QUOTED" and out_atoms is not None else 1


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay historical RPC trace through ReplayAdapter and output 8 tiers."""
    from research.quoting.quoter import run_historical_replay
    from research.quoting.replay_adapter import ReplayAdapter

    rpc_file = getattr(args, "rpc_file", None)
    print("[REPLAY] Source: explicit historical file" if rpc_file else "[REPLAY] Source: bundled historical fixture; offline, not live market data")
    adapter = ReplayAdapter(records_or_path=rpc_file)
    results = run_historical_replay(adapter)

    print(
        f"[REPLAY] Completed historical replay of {adapter.total_count} RPC records "
        f"across {len(results)} tiers:"
    )
    for idx, res in enumerate(results, start=1):
        err = f" ({res.error_message})" if res.error_message else ""
        print(
            f"  Tier {idx}: status={res.status.value}, "
            f"in={res.amount_in.atoms} {res.amount_in.token.symbol}{err}"
        )

    print(
        f"[REPLAY] Consumed: {adapter.consumed_count()}/{adapter.total_count} records. "
        f"Finished: {adapter.is_finished()}"
    )
    return 0 if adapter.is_finished() and results and all(res.status.value != "RPC_ERROR" for res in results) else 1


def _run_explicit_simulation(args: argparse.Namespace) -> int:
    """Delegate explicit offline input to the existing simulation pipeline."""
    source = getattr(args, "input", None)
    if not source:
        sys.stderr.write("[INPUT_INSUFFICIENT] Explicit --input JSONL is required; no default plan is fabricated.\n")
        return 1
    if getattr(args, "rpc", None) or getattr(args, "private_key", None):
        sys.stderr.write("[SECURITY INTERCEPT] Offline simulation does not accept RPC endpoints or private keys.\n")
        return 1
    from apps.atomic_simulate import main as simulate_main

    forwarded = ["--mode", "offline", "--input", source]
    for option, attr in (("--output", "output"), ("--summary-output", "summary_output"), ("--caller", "caller")):
        value = getattr(args, attr, None)
        if value is not None:
            forwarded.extend([option, value])
    return simulate_main(forwarded)


def cmd_simulate(args: argparse.Namespace) -> int:
    """Evaluate an explicit offline candidate stream without creating a trade."""
    return _run_explicit_simulation(args)


def cmd_trade(args: argparse.Namespace) -> int:
    """Dispatch trade plan with mandatory --dry-run security guardrails."""
    # Mandatory Safety Gate 1: dry-run must be explicitly active
    if not getattr(args, "dry_run", False):
        sys.stderr.write(
            "[SECURITY INTERCEPT] Live trade execution blocked: --dry-run is mandatory. "
            "Real on-chain broadcasting is strictly prohibited in this uncredentialed isolation sandbox.\n"
        )
        raise AuthorizationBlockedError(
            "Live trade execution blocked: --dry-run is mandatory. "
            "Real on-chain broadcasting is prohibited without authorization."
        )

    # Mandatory Safety Gate 2: In uncredentialed sandbox, live credentials / private keys are absent
    if getattr(args, "require_key", False) and not getattr(args, "private_key", None):
        sys.stderr.write(
            "[SECURITY INTERCEPT] Live trade execution blocked: no authorized private key credentials provided.\n"
        )
        raise AuthorizationBlockedError(
            "Live trade execution blocked: no authorized private key credentials provided."
        )

    return _run_explicit_simulation(args)


# -----------------------------------------------------------------------------
# CLI Parser Construction & Main Entrypoint
# -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build unified argument parser with subcommands and parameter descriptions."""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Unified thin entrypoint and CLI assembly for DEX arbitrage and execution.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Subcommand to execute",
    )

    # 1. monitor
    p_monitor = subparsers.add_parser(
        "monitor",
        help="Run read-only market monitor service (strictly isolated from execution)",
    )
    p_monitor.add_argument("--rpc", type=str, default=None, help="RPC endpoint URL")
    p_monitor.add_argument(
        "--min-gross-bps",
        type=float,
        default=10.0,
        help="Minimum gross profit in bps (default: 10.0)",
    )
    p_monitor.add_argument(
        "--base-token",
        type=str,
        default="USDG",
        help="Base token symbol (default: USDG)",
    )
    p_monitor.add_argument(
        "--block",
        type=int,
        default=None,
        help="Target block number for market snapshot",
    )
    p_monitor.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Execute single polling cycle and exit (default: True)",
    )
    p_monitor.set_defaults(func=cmd_monitor)

    # 2. quote
    p_quote = subparsers.add_parser(
        "quote",
        help="Assemble Quoter and MarketSnapshot to query DEX quotes",
    )
    p_quote.add_argument("--rpc", type=str, default=None, help="RPC endpoint URL")
    p_quote.add_argument(
        "--block",
        type=int,
        default=None,
        help="Target block number for quote",
    )
    p_quote.add_argument(
        "--route",
        choices=["canonical_a", "canonical_b"],
        default="canonical_a",
        help="Predefined canonical route to quote (default: canonical_a)",
    )
    p_quote.add_argument(
        "--amount-in",
        type=int,
        default=10**15,
        help="Input token amount in atoms (default: 10^15)",
    )
    p_quote.set_defaults(func=cmd_quote)

    # 3. replay
    p_replay = subparsers.add_parser(
        "replay",
        help="Replay historical RPC trace through ReplayAdapter (offline 30 RPC calls, 8 tiers)",
    )
    p_replay.add_argument(
        "--rpc-file",
        type=str,
        default=None,
        help="Path to JSONL historical RPC evidence file",
    )
    p_replay.set_defaults(func=cmd_replay)

    # 4. simulate
    p_simulate = subparsers.add_parser(
        "simulate",
        help="Evaluate explicit offline JSONL input; no live RPC or broadcasting",
    )
    p_simulate.add_argument("--plan-id", type=str, default="sim_plan_01", help="Plan identifier")
    p_simulate.add_argument(
        "--base",
        choices=["WETH", "USDG"],
        default="WETH",
        help="Base token symbol",
    )
    p_simulate.add_argument(
        "--amount",
        type=int,
        default=10**16,
        help="Input amount in atoms (default: 10^16)",
    )
    p_simulate.add_argument(
        "--wallet",
        type=str,
        default="0x1111111111111111111111111111111111111111",
        help="Sender wallet address",
    )
    p_simulate.add_argument("--rpc", type=str, default=None, help="RPC endpoint URL")
    p_simulate.add_argument(
        "--ledger-db",
        type=str,
        default="/tmp/cli_execution_ledger.sqlite",
        help="SQLite ledger path",
    )
    p_simulate.add_argument("--input", default=None, help="Explicit offline simulation input")
    p_simulate.add_argument("--output", default=None, help="Explicit offline simulation output")
    p_simulate.add_argument("--summary-output", default=None, help="Explicit offline simulation summary-output")
    p_simulate.add_argument("--caller", default=None, help="Explicit offline simulation caller")
    p_simulate.set_defaults(func=cmd_simulate)

    # 5. trade
    p_trade = subparsers.add_parser(
        "trade",
        help="Dispatch trade plan (requires --dry-run; live broadcast prohibited)",
    )
    p_trade.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Mandatory dry-run simulation mode (real on-chain broadcast blocked)",
    )
    p_trade.add_argument(
        "--private-key",
        type=str,
        default=None,
        help="Signer private key (strictly unauthorized in sandbox)",
    )
    p_trade.add_argument(
        "--require-key",
        action="store_true",
        default=False,
        help="Enforce private key presence check",
    )
    p_trade.add_argument(
        "--plan-id",
        type=str,
        default="trade_dry_run_01",
        help="Plan identifier",
    )
    p_trade.add_argument(
        "--base",
        choices=["WETH", "USDG"],
        default="WETH",
        help="Base token symbol",
    )
    p_trade.add_argument(
        "--amount",
        type=int,
        default=10**16,
        help="Input amount in atoms (default: 10^16)",
    )
    p_trade.add_argument(
        "--wallet",
        type=str,
        default="0x1111111111111111111111111111111111111111",
        help="Sender wallet address",
    )
    p_trade.add_argument("--rpc", type=str, default=None, help="RPC endpoint URL")
    p_trade.add_argument(
        "--ledger-db",
        type=str,
        default="/tmp/cli_execution_ledger.sqlite",
        help="SQLite ledger path",
    )
    p_trade.add_argument("--input", default=None, help="Explicit offline simulation input")
    p_trade.add_argument("--output", default=None, help="Explicit offline simulation output")
    p_trade.add_argument("--summary-output", default=None, help="Explicit offline simulation summary-output")
    p_trade.add_argument("--caller", default=None, help="Explicit offline simulation caller")
    p_trade.set_defaults(func=cmd_trade)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    exit_on_error: bool = False,
) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except Exception as exc:
        if type(exc).__name__ == "AuthorizationBlockedError":
            sys.stderr.write(f"\n[CRITICAL SAFETY INTERCEPT] {exc}\n")
            if exit_on_error:
                sys.exit(1)
            return 1
        raise


if __name__ == "__main__":
    try:
        sys.exit(main(exit_on_error=True))
    except SystemExit:
        raise
    except Exception as _exc:
        if type(_exc).__name__ == "AuthorizationBlockedError":
            sys.stderr.write(f"\n[CRITICAL SAFETY INTERCEPT] {_exc}\n")
            sys.exit(1)
        raise
