"""Arc Market Structure Event Classification and Priority Rules (T33)

Enforces:
- Normalized event typing: SecondVenueCreated, LiquidityChanged, GraduationMilestone, LiquidityMigrated
- Strict token address identity: tokens are identified by contract address, NEVER ticker symbols (anti-impersonation)
- Frontend alias rejection: same pool address with different alias/name is NOT a second venue
- Milestone events do NOT emit buy triggers (strictly recalculate priority or observe)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MarketEventType(StrEnum):
    SECOND_VENUE_CREATED = "SecondVenueCreated"
    LIQUIDITY_CHANGED = "LiquidityChanged"
    GRADUATION_MILESTONE = "GraduationMilestone"
    LIQUIDITY_MIGRATED = "LiquidityMigrated"


class PriorityLevel(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EventAction(StrEnum):
    RECALCULATE_PRIORITY = "recalculate_priority"
    INVALIDATE_OLD_CURVE = "invalidate_old_curve"
    OBSERVE_ONLY = "observe_only"


@dataclass(frozen=True, slots=True)
class EventRuleDecision:
    """Evaluated outcome for a market structure event."""

    event_type: MarketEventType
    priority: PriorityLevel
    action: EventAction
    affected_tokens: tuple[str, ...]
    is_valid_second_venue: bool
    confidence_score: float
    audit_notes: tuple[str, ...]


class EventEvaluationRules:
    """Evaluates market structure events against security and priority invariants."""

    @staticmethod
    def evaluate_second_venue(
        token_a: str,
        token_b: str,
        existing_venue_pools: dict[str, str],  # venue_name -> pool_address
        new_venue_name: str,
        new_pool_address: str,
    ) -> EventRuleDecision:
        """Evaluate whether a new pool constitutes a genuinely distinct second venue.

        Invariants:
        1. Token addresses must be compared strictly by lowercase 42-char EVM address.
        2. If new_pool_address matches an existing pool address, it is a frontend alias, NOT a second venue!
        """
        token_a_norm = token_a.lower()
        token_b_norm = token_b.lower()
        new_pool_norm = new_pool_address.lower()

        notes: list[str] = []

        # Check for frontend alias
        existing_addrs = {p.lower() for p in existing_venue_pools.values()}
        if new_pool_norm in existing_addrs:
            notes.append("Rejected as duplicate frontend alias; underlying pool address matches existing venue")
            return EventRuleDecision(
                event_type=MarketEventType.SECOND_VENUE_CREATED,
                priority=PriorityLevel.LOW,
                action=EventAction.OBSERVE_ONLY,
                affected_tokens=(token_a_norm, token_b_norm),
                is_valid_second_venue=False,
                confidence_score=0.0,
                audit_notes=tuple(notes),
            )

        notes.append("Distinct verified venue detected; promoting recalculation priority to HIGH")
        return EventRuleDecision(
            event_type=MarketEventType.SECOND_VENUE_CREATED,
            priority=PriorityLevel.HIGH,
            action=EventAction.RECALCULATE_PRIORITY,
            affected_tokens=(token_a_norm, token_b_norm),
            is_valid_second_venue=True,
            confidence_score=0.95,
            audit_notes=tuple(notes),
        )

    @staticmethod
    def evaluate_graduation_milestone(
        curve_pool_address: str,
        token_address: str,
        bonding_progress_bps: int,
    ) -> EventRuleDecision:
        """Evaluate bonding curve progress milestones.

        CRITICAL INVARIANT: High bonding progress is NEVER an automated buy signal.
        Action is strictly OBSERVE_ONLY.
        """
        notes = [
            f"Bonding progress reached {bonding_progress_bps} bps",
            "Automated buy orders strictly disallowed on launchpad milestones",
        ]
        priority = PriorityLevel.NORMAL if bonding_progress_bps >= 9000 else PriorityLevel.LOW
        return EventRuleDecision(
            event_type=MarketEventType.GRADUATION_MILESTONE,
            priority=priority,
            action=EventAction.OBSERVE_ONLY,
            affected_tokens=(token_address.lower(),),
            is_valid_second_venue=False,
            confidence_score=1.0,
            audit_notes=tuple(notes),
        )

    @staticmethod
    def evaluate_liquidity_migrated(
        source_curve_address: str,
        target_amm_address: str,
        token_address: str,
    ) -> EventRuleDecision:
        """Evaluate liquidity migration event from curve to primary AMM."""
        notes = [
            f"Liquidity migrated from curve {source_curve_address} to AMM {target_amm_address}",
            "Invalidating old curve routing and triggering immediate priority recalculation",
        ]
        return EventRuleDecision(
            event_type=MarketEventType.LIQUIDITY_MIGRATED,
            priority=PriorityLevel.CRITICAL,
            action=EventAction.INVALIDATE_OLD_CURVE,
            affected_tokens=(token_address.lower(),),
            is_valid_second_venue=True,
            confidence_score=1.0,
            audit_notes=tuple(notes),
        )
