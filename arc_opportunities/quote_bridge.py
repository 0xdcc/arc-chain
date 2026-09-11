"""Arc Single-Segment CLMM Quote Bridge and Graph Engine (T19)

Adapts W4 discrete Q64.96 integer CLMM quoting and cycle discovery to Arc chain (5042 L1).
Enforces:
- Strict single_segment capability tag and F01 cross-tick rejection preservation
- Explicit token decimal resolution (0, 6, 18 decimals supported)
- Mandatory Arc L1 block domain and chain_id validation (5042, 5042002)
- Zero floating-point drift: discrete integer atom propagation
- Zero-hook validation: rejects unverified or dynamic hooks in single_segment mode
- Truthful negative delta recording without pruning
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.arc_extensions import BlockDomain
from arbitrage_contracts.identity import Amount, AssetRef, PoolDescriptor
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EvidenceLevel,
    HopQuote,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.state import canonical_state_ref
from state_graph.cycles import find_cycles
from state_graph.evaluate import (
    evaluate_route_exact_input,
    resolve_token_decimals,
    single_segment_target,
)
from state_graph.graph import PoolGraph
from state_graph.types import FrozenEpoch

CAPABILITY_SINGLE_SEGMENT = "single_segment"
SUPPORTED_CHAIN_IDS = frozenset({5042, 5042002})


class ArcQuoteBridgeError(ValueError):
    """Raised for malformed quote bridge configuration or inputs."""


@dataclass(frozen=True, slots=True)
class ArcQuoteConfig:
    """Configuration for Arc quoting engine."""

    chain_id: int = 5042
    block_domain: BlockDomain = BlockDomain.L1
    capability: str = CAPABILITY_SINGLE_SEGMENT
    max_hops: int = 3

    def __post_init__(self) -> None:
        if self.chain_id not in SUPPORTED_CHAIN_IDS:
            raise ArcQuoteBridgeError(f"Unsupported Arc chain_id: {self.chain_id}")
        if self.block_domain != BlockDomain.L1:
            raise ArcQuoteBridgeError(f"Arc quoting requires L1 block domain, got: {self.block_domain}")


class ArcQuoteBridge:
    """Arc Single-Segment Quote and Graph Bridge."""

    def __init__(self, config: ArcQuoteConfig | None = None) -> None:
        self.config = config if config is not None else ArcQuoteConfig()

    def build_graph(self, pools: Sequence[PoolDescriptor]) -> PoolGraph:
        """Construct a directed multigraph filtering only deployed pools matching Arc chain."""
        graph = PoolGraph()
        for pool in pools:
            if pool.key.chain_id == self.config.chain_id and pool.deployment_status == "deployed":
                graph.add_pool(pool)
        return graph

    def find_routes(
        self,
        graph: PoolGraph,
        base_assets: Sequence[AssetRef],
        allowed_hops: Sequence[int] = (2, 3),
        max_routes: int | None = None,
    ) -> list[RouteRef]:
        """Find bounded 2-hop and 3-hop closed arbitrage cycles anchored at base assets."""
        arc_base_assets = [a for a in base_assets if a.chain_id == self.config.chain_id]
        return find_cycles(
            graph=graph,
            base_assets=arc_base_assets,
            allowed_hops=allowed_hops,
            max_routes=max_routes,
        )

    def quote_exact_input(
        self,
        route: RouteRef,
        amount_in: Amount,
        epoch: FrozenEpoch,
        quote_id: str | None = None,
        data_mode: str = DataMode.SYNTHETIC,
        actor_scope: str = ActorScope.OWN_AUTHORIZED,
        token_decimals: Mapping[Any, int] | None = None,
    ) -> QuoteEvidence:
        """Evaluate route exact input on Arc with fail-closed safety and capability tags.

        Guarantees:
        1. Chain and domain check: route and state_version must match Arc chain_id and L1 domain.
        2. F01 cross-tick preservation: cross-tick swaps return UNSUPPORTED with clean reason.
        3. Hook safety: non-zero or dynamic hooks are rejected as UNSUPPORTED.
        4. Atom parity: discrete integer atoms match Q64.96 CLMM math exactly.
        """
        # Guard 1: Verify chain identity
        if route.chain_id != self.config.chain_id:
            return self._make_unsupported_quote(
                route=route,
                amount_in=amount_in,
                epoch=epoch,
                error_msg=f"Route chain_id {route.chain_id} does not match bridge chain_id {self.config.chain_id}",
                quote_id=quote_id,
                data_mode=data_mode,
                actor_scope=actor_scope,
            )

        # Guard 2: Verify StateVersion domain and chain
        sv = epoch.state_version
        if sv.chain_id != self.config.chain_id:
            return self._make_unsupported_quote(
                route=route,
                amount_in=amount_in,
                epoch=epoch,
                error_msg=f"StateVersion chain_id {sv.chain_id} does not match bridge {self.config.chain_id}",
                quote_id=quote_id,
                data_mode=data_mode,
                actor_scope=actor_scope,
            )

        if sv.block_domain != "l1":
            return self._make_unsupported_quote(
                route=route,
                amount_in=amount_in,
                epoch=epoch,
                error_msg=f"Arc requires L1 block domain, got: {sv.block_domain}",
                quote_id=quote_id,
                data_mode=data_mode,
                actor_scope=actor_scope,
            )

        # Guard 3: Zero-hook / static fee model check for single_segment
        for idx, hop in enumerate(route.hops):
            desc = hop.pool_descriptor
            if desc is not None:
                if desc.hooks is not None and desc.hooks != "" and int(desc.hooks, 16) != 0:
                    return self._make_unsupported_quote(
                        route=route,
                        amount_in=amount_in,
                        epoch=epoch,
                        error_msg=f"Hop {idx} declared hooks {desc.hooks}; hooks are UNSUPPORTED in single_segment",
                        quote_id=quote_id,
                        data_mode=data_mode,
                        actor_scope=actor_scope,
                    )
                if desc.fee_model.kind != "static":
                    return self._make_unsupported_quote(
                        route=route,
                        amount_in=amount_in,
                        epoch=epoch,
                        error_msg=f"Hop {idx} declared fee model {desc.fee_model.kind}; dynamic fee is UNSUPPORTED in single_segment",
                        quote_id=quote_id,
                        data_mode=data_mode,
                        actor_scope=actor_scope,
                    )

        # Delegate to underlying evaluate_route_exact_input
        evidence = evaluate_route_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            quote_id=quote_id,
            data_mode=data_mode,
            actor_scope=actor_scope,
            token_decimals=token_decimals,
        )
        return evidence

    def _make_unsupported_quote(
        self,
        route: RouteRef,
        amount_in: Amount,
        epoch: FrozenEpoch,
        error_msg: str,
        quote_id: str | None = None,
        data_mode: str = DataMode.SYNTHETIC,
        actor_scope: str = ActorScope.OWN_AUTHORIZED,
    ) -> QuoteEvidence:
        started_at = int(time.time() * 1000)
        try:
            state_ref = canonical_state_ref(epoch.state_version)
        except Exception:
            state_ref = None

        if quote_id is None:
            seed = f"{route.route_id}:{epoch.epoch_id}:{amount_in.atoms}:{started_at}"
            quote_id = f"arc-quote:{hashlib.sha256(seed.encode()).hexdigest()[:16]}"

        return QuoteEvidence(
            quote_id=quote_id,
            route_ref=route,
            amount_in=amount_in,
            amount_out=None,
            delta_atoms=None,
            hop_quotes=(),
            state_version_ref=state_ref,
            started_at_ms=started_at,
            finished_at_ms=int(time.time() * 1000),
            status=QuoteStatus.UNSUPPORTED,
            evidence_level=EvidenceLevel.LOCAL_QUOTE,
            data_mode=data_mode,
            actor_scope=actor_scope,
            fee_included=TriState.UNKNOWN,
            impact_included=TriState.UNKNOWN,
            error=error_msg,
        )
