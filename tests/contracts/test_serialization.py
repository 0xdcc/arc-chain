"""Unit tests verifying canonical JSON serialization, schema validation, and hashing (A23)."""

from __future__ import annotations

import json
import unittest

from arbitrage_contracts.eligibility import (
    AssetEligibility,
    EvidenceLevel,
    PoolCapability,
    SourceEvidence,
)
from arbitrage_contracts.identity import (
    Amount,
    AssetRef,
    FeeModel,
    PoolDescriptor,
    PoolKey,
    TokenKey,
)
from arbitrage_contracts.opportunity import (
    Observation,
    OpportunityPhase,
    OpportunityRecord,
)
from arbitrage_contracts.quote import (
    ActorScope,
    DataMode,
    EconomicAssessment,
    EconomicStatus,
    FeeComponent,
    GasEvidence,
    GasEvidenceKind,
    HopDirection,
    HopQuote,
    HopRef,
    PriceEvidence,
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
    TriState,
)
from arbitrage_contracts.serialization import (
    ContractRecord,
    canonical_record_hash,
    decode_record_json,
    encode_record_json,
    validate_record,
)
from arbitrage_contracts.state import Cursor, StateVersion


class TestSerializationAndHashing(unittest.TestCase):
    """A23: Canonical JSON serialization, roundtrip fidelity, and deterministic hashing."""

    def setUp(self) -> None:
        self.chain_id = 4663
        self.token_a = AssetRef.erc20(
            TokenKey(self.chain_id, "0x1111111111111111111111111111111111111111")
        )
        self.token_b = AssetRef.erc20(
            TokenKey(self.chain_id, "0x2222222222222222222222222222222222222222")
        )
        self.pool_ab = PoolKey(
            self.chain_id,
            "uniswap_v3",
            "factory",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "address",
            "0xab00000000000000000000000000000000000001",
        )
        self.pool_ba = PoolKey(
            self.chain_id,
            "sushiswap",
            "factory",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "address",
            "0xba00000000000000000000000000000000000002",
        )
        self.hop_ab = HopRef(self.pool_ab, self.token_a, self.token_b)
        self.hop_ba = HopRef(self.pool_ba, self.token_b, self.token_a, HopDirection.ONE_FOR_ZERO)
        self.route = RouteRef(self.chain_id, self.token_a, (self.hop_ab, self.hop_ba))
        self.amount_in = Amount.from_atoms_str(self.token_a, "1000000", 6)
        self.amount_out = Amount.from_atoms_str(self.token_a, "1040000", 6)

        self.gas_evidence = GasEvidence(
            gas_kind=GasEvidenceKind.RPC_ESTIMATE,
            gas_units=120000,
            gas_price_atoms=2000000000,
        )
        self.fee_comp = FeeComponent("pool_fee_0", 500, self.token_a)
        self.assessment = EconomicAssessment(
            net_atoms=39500,
            net_usd_micros=39500,
            economic_status=EconomicStatus.PROFITABLE,
            fee_components=(self.fee_comp,),
            gas_cost_atoms=240,
        )

        self.quote = QuoteEvidence(
            quote_id="quote-ser-01",
            route_ref=self.route,
            amount_in=self.amount_in,
            amount_out=self.amount_out,
            status=QuoteStatus.QUOTED,
            evidence_level=EvidenceLevel.LOCAL_QUOTE,
            data_mode=DataMode.SYNTHETIC,
            actor_scope=ActorScope.SYNTHETIC,
            fee_included=TriState.YES,
            impact_included=TriState.YES,
            gas_evidence=self.gas_evidence,
            economic_assessment=self.assessment,
        )

        self.quote_record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="quote_evidence",
            run_id="run-baseline-001",
            data_mode=DataMode.SYNTHETIC,
            provenance={"generator": "test_suite", "collector_version": "1.0.0"},
            payload=self.quote,
        )

    def test_quote_record_roundtrip(self) -> None:
        """Verify encode -> decode -> encode produces strictly invariant JSON and hash."""
        json_text = encode_record_json(self.quote_record)
        self.assertIsInstance(json_text, str)
        self.assertIn('"atoms":"1000000"', json_text)
        self.assertIn('"net_atoms":"39500"', json_text)

        # Decode back to strongly typed ContractRecord
        decoded = decode_record_json(json_text)
        self.assertEqual(decoded.schema_id, "arbitrage-evidence")
        self.assertEqual(decoded.record_type, "quote_evidence")
        self.assertIsInstance(decoded.payload, QuoteEvidence)
        self.assertEqual(decoded.payload.amount_in.atoms, 1000000)
        self.assertEqual(decoded.payload.amount_out.atoms, 1040000)
        self.assertEqual(decoded.payload.delta_atoms, 40000)
        self.assertEqual(decoded.payload.economic_assessment.net_atoms, 39500)

        # Re-encode and assert exact byte equality and hash equality
        re_encoded = encode_record_json(decoded)
        self.assertEqual(json_text, re_encoded)

        hash1 = canonical_record_hash(self.quote_record)
        hash2 = canonical_record_hash(decoded)
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)

    def test_opportunity_record_roundtrip(self) -> None:
        """Verify OpportunityRecord roundtrip encodes and decodes perfectly."""
        obs = Observation(
            observation_id="obs-001",
            run_id="run-baseline-001",
            observed_at_ms=1050,
            phase=OpportunityPhase.QUOTED,
            quote_evidence=self.quote,
            result="profitable",
        )
        opp = OpportunityRecord(
            opportunity_id="opp-episode-01",
            route_id=self.route.route_id,
            amount_in=self.amount_in,
            first_seen_at_ms=1000,
            last_rechecked_at_ms=1050,
            last_seen_at_ms=1100,
            phase=OpportunityPhase.QUOTED,
            observations=(obs,),
            data_mode=DataMode.SYNTHETIC,
            actor_scope=ActorScope.SYNTHETIC,
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="opportunity_record",
            run_id="run-baseline-001",
            data_mode=DataMode.SYNTHETIC,
            provenance={"generator": "test_suite"},
            payload=opp,
        )

        encoded = encode_record_json(record)
        decoded = decode_record_json(encoded)
        self.assertIsInstance(decoded.payload, OpportunityRecord)
        self.assertEqual(decoded.payload.opportunity_id, "opp-episode-01")
        self.assertEqual(len(decoded.payload.observations), 1)
        self.assertEqual(decoded.payload.observations[0].quote_evidence.amount_out.atoms, 1040000)
        self.assertEqual(encode_record_json(decoded), encoded)

    def test_reject_duplicate_json_keys(self) -> None:
        """Verify parser rejects duplicate JSON keys fail-closed."""
        duplicate_key_json = """
        {
            "schema_id": "arbitrage-evidence",
            "schema_version": "1.0.0",
            "record_type": "token_key",
            "run_id": "run-1",
            "data_mode": "synthetic",
            "provenance": {},
            "payload": {"chain_id": 1, "address": "0x1111111111111111111111111111111111111111"},
            "run_id": "run-2"
        }
        """
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(duplicate_key_json)
        self.assertIn("Duplicate JSON key detected", str(ctx.exception))

    def test_reject_prohibited_float_nan_inf(self) -> None:
        """Verify parser and serializer reject float NaN and Infinity."""
        nan_json = """
        {
            "schema_id": "arbitrage-evidence",
            "schema_version": "1.0.0",
            "record_type": "token_key",
            "run_id": "run-1",
            "data_mode": "synthetic",
            "provenance": {"score": NaN},
            "payload": {"chain_id": 1, "address": "0x1111111111111111111111111111111111111111"}
        }
        """
        with self.assertRaises(ValueError):
            decode_record_json(nan_json)

        inf_json = """
        {
            "schema_id": "arbitrage-evidence",
            "schema_version": "1.0.0",
            "record_type": "token_key",
            "run_id": "run-1",
            "data_mode": "synthetic",
            "provenance": {"score": Infinity},
            "payload": {"chain_id": 1, "address": "0x1111111111111111111111111111111111111111"}
        }
        """
        with self.assertRaises(ValueError):
            decode_record_json(inf_json)

    def test_reject_unknown_record_type_or_schema_version(self) -> None:
        """Verify unknown record_type and schema_version are rejected fail-closed."""
        bad_type_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "unauthorized_malicious_type",
                "run_id": "run-1",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {},
            }
        )
        with self.assertRaises(ValueError):
            decode_record_json(bad_type_json)

        bad_version_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "2.0.0",
                "record_type": "quote_evidence",
                "run_id": "run-1",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {},
            }
        )
        with self.assertRaises(ValueError):
            decode_record_json(bad_version_json)

    def test_hash_invariance_to_key_order(self) -> None:
        """Verify canonical_record_hash is strictly invariant to original dict key ordering."""
        dict_order_1 = {
            "schema_id": "arbitrage-evidence",
            "schema_version": "1.0.0",
            "record_type": "token_key",
            "run_id": "run-1",
            "data_mode": "synthetic",
            "provenance": {"a": 1, "b": 2},
            "payload": {"chain_id": 4663, "address": "0x1111111111111111111111111111111111111111"},
        }
        dict_order_2 = {
            "payload": {"address": "0x1111111111111111111111111111111111111111", "chain_id": 4663},
            "provenance": {"b": 2, "a": 1},
            "data_mode": "synthetic",
            "run_id": "run-1",
            "record_type": "token_key",
            "schema_version": "1.0.0",
            "schema_id": "arbitrage-evidence",
        }
        rec1 = validate_record(dict_order_1)
        rec2 = validate_record(dict_order_2)
        self.assertEqual(encode_record_json(rec1), encode_record_json(rec2))
        self.assertEqual(canonical_record_hash(rec1), canonical_record_hash(rec2))

    def test_source_evidence_roundtrip(self) -> None:
        """Verify SourceEvidence roundtrip encodes and decodes to strongly typed instance."""
        evidence = SourceEvidence(
            evidence_id="ev-chain-1234",
            source_type="eth_call",
            source_locator="https://rpc.robinhood.com",
            raw_sha256="abcdef1234567890" * 4,
            captured_at_ms=1725900000000,
            chain_id=4663,
            block_ref="0x" + "11" * 32,
            request_id="req-99",
            params_hash="params-hash-01",
            collector_version="1.2.0",
            limitations=("sample_only", "no_reorg_guarantee"),
            parent_refs=("ev-parent-001",),
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="source_evidence",
            run_id="run-ev-001",
            data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
            provenance={"witness": "node_audit"},
            payload=evidence,
        )
        encoded = encode_record_json(record)
        decoded = decode_record_json(encoded)
        self.assertIsInstance(decoded.payload, SourceEvidence)
        self.assertEqual(decoded.payload.evidence_id, "ev-chain-1234")
        self.assertEqual(decoded.payload.raw_sha256, "abcdef1234567890" * 4)
        self.assertEqual(decoded.payload.limitations, ("sample_only", "no_reorg_guarantee"))
        self.assertEqual(encode_record_json(decoded), encoded)
        self.assertEqual(canonical_record_hash(record), canonical_record_hash(decoded))

    def test_asset_eligibility_roundtrip(self) -> None:
        """Verify AssetEligibility roundtrip encodes and decodes to strongly typed instance."""
        eligibility = AssetEligibility(
            asset_ref=self.token_a,
            issuer_id="issuer-test-vault",
            issuance_or_bridge_version="v2.1",
            decimals_status="verified",
            decimals_evidence_ref="ev-decimals-01",
            contract_restrictions={"pausable": "verified_false", "taxable": "verified_false"},
            review_status="approved",
            reviewer_ref="security-team-alice",
            reviewed_at_ms=1725900000000,
            validity="epoch-5000",
            subject_scope="public",
            evidence_refs=("ev-audit-001", "ev-audit-002"),
            reasons=("code_audited", "liquidity_adequate"),
            registry_revision="rev-2026-09-09",
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="asset_eligibility",
            run_id="run-elig-001",
            data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
            provenance={"verifier": "w1_market_catalog"},
            payload=eligibility,
        )
        encoded = encode_record_json(record)
        decoded = decode_record_json(encoded)
        self.assertIsInstance(decoded.payload, AssetEligibility)
        self.assertTrue(decoded.payload.is_approved())
        self.assertEqual(decoded.payload.get_restriction("pausable"), "verified_false")
        self.assertEqual(decoded.payload.get_restriction("taxable"), "verified_false")
        self.assertEqual(decoded.payload.get_restriction("unknown_key"), "unknown")
        self.assertEqual(decoded.payload.reviewer_ref, "security-team-alice")
        self.assertEqual(encode_record_json(decoded), encoded)
        self.assertEqual(canonical_record_hash(record), canonical_record_hash(decoded))

    def test_pool_capability_roundtrip(self) -> None:
        """Verify PoolCapability roundtrip encodes and decodes to strongly typed instance."""
        capability = PoolCapability(
            pool_key=self.pool_ab,
            can_quote="supported",
            can_simulate="supported",
            can_atomic_execute="unsupported",
            evidence_refs=("ev-quote-001",),
            reasons=("quoter_tested", "router_missing"),
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="pool_capability",
            run_id="run-cap-001",
            data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
            provenance={"verifier": "w1_market_catalog"},
            payload=capability,
        )
        encoded = encode_record_json(record)
        decoded = decode_record_json(encoded)
        self.assertIsInstance(decoded.payload, PoolCapability)
        self.assertEqual(decoded.payload.can_quote, "supported")
        self.assertEqual(decoded.payload.can_simulate, "supported")
        self.assertEqual(decoded.payload.can_atomic_execute, "unsupported")
        self.assertEqual(decoded.payload.pool_key, self.pool_ab)
        self.assertEqual(encode_record_json(decoded), encoded)
        self.assertEqual(canonical_record_hash(record), canonical_record_hash(decoded))

    def test_reject_bad_fields_in_eligibility_and_capability(self) -> None:
        """Verify bad fields or illegal status strings are rejected fail-closed during decoding."""
        # 1. Invalid review_status in asset_eligibility
        bad_elig_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "asset_eligibility",
                "run_id": "run-bad-01",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {
                    "asset_ref": {
                        "address": "0x1111111111111111111111111111111111111111",
                        "chain_id": 4663,
                        "interface_kind": "erc20",
                    },
                    "review_status": "unauthorized_custom_status",
                },
            }
        )
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(bad_elig_json)
        self.assertIn("Invalid review_status", str(ctx.exception))

        # 2. Approved asset missing reviewer_ref
        unapproved_lack_reviewer_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "asset_eligibility",
                "run_id": "run-bad-02",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {
                    "asset_ref": {
                        "address": "0x1111111111111111111111111111111111111111",
                        "chain_id": 4663,
                        "interface_kind": "erc20",
                    },
                    "review_status": "approved",
                    "reviewed_at_ms": 1725900000000,
                    "evidence_refs": ["ev-1"],
                    "reviewer_ref": None,
                },
            }
        )
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(unapproved_lack_reviewer_json)
        self.assertIn("Approved asset must specify a non-empty reviewer_ref", str(ctx.exception))

        # 3. Invalid capability status in pool_capability
        bad_cap_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "pool_capability",
                "run_id": "run-bad-03",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {
                    "pool_key": {
                        "chain_id": 4663,
                        "protocol_id": "uniswap_v3",
                        "venue_kind": "factory",
                        "venue_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "pool_id_kind": "address",
                        "pool_id": "0xab00000000000000000000000000000000000001",
                    },
                    "can_quote": "maybe_supported",
                },
            }
        )
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(bad_cap_json)
        self.assertIn("Invalid status for can_quote", str(ctx.exception))

        # 4. Invalid raw_sha256 in source_evidence
        bad_evidence_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "source_evidence",
                "run_id": "run-bad-04",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {
                    "evidence_id": "ev-bad-hash",
                    "source_type": "eth_call",
                    "source_locator": "https://rpc.example.com",
                    "raw_sha256": "not_a_64_character_hex_hash",
                },
            }
        )
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(bad_evidence_json)
        self.assertIn("Invalid SHA256 hex string", str(ctx.exception))

        # 5. Type confusion: payload is not a Mapping or expected dataclass
        bad_payload_type_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "asset_eligibility",
                "run_id": "run-bad-05",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": "string_payload_is_illegal",
            }
        )
        with self.assertRaises(TypeError) as type_ctx:
            decode_record_json(bad_payload_type_json)
        self.assertIn(
            "payload for asset_eligibility must be Mapping or AssetEligibility", str(type_ctx.exception)
        )

    def test_state_version_with_applied_cursor_roundtrip(self) -> None:
        """Verify StateVersion with non-null applied_cursor roundtrip encodes and decodes perfectly."""
        cursor = Cursor(
            block_hash="0x" + "aa" * 32,
            transaction_hash="0x" + "bb" * 32,
            transaction_index=1,
            log_index=2,
        )
        state_ver = StateVersion(
            chain_id=4663,
            block_number=58239320,
            block_hash="0x" + "cc" * 32,
            received_at_ms=1725900010000,
            block_domain="l2",
            parent_hash="0x" + "dd" * 32,
            epoch_id="epoch-4663-001",
            source_ref="rpc_feed_01",
            block_timestamp_s=1725900010,
            applied_cursor=cursor,
            complete_through_block=58239320,
            completeness="ready",
            coverage=("pool-1", "pool-2"),
            stale_reasons=(),
            finality="safe",
            finality_evidence_ref="ev-finality-01",
            optional_l1_anchor="0x" + "ee" * 32,
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="state_version",
            run_id="run-state-001",
            data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
            provenance={"witness": "w4_state_graph"},
            payload=state_ver,
        )

        encoded = encode_record_json(record)
        self.assertIn('"applied_cursor":', encoded)
        self.assertIn('"log_index":2', encoded)
        self.assertIn('"transaction_index":1', encoded)

        decoded = decode_record_json(encoded)
        self.assertIsInstance(decoded.payload, StateVersion)
        self.assertIsNotNone(decoded.payload.applied_cursor)
        self.assertIsInstance(decoded.payload.applied_cursor, Cursor)
        self.assertEqual(decoded.payload.applied_cursor.block_hash, "0x" + "aa" * 32)
        self.assertEqual(decoded.payload.applied_cursor.transaction_hash, "0x" + "bb" * 32)
        self.assertEqual(decoded.payload.applied_cursor.transaction_index, 1)
        self.assertEqual(decoded.payload.applied_cursor.log_index, 2)
        self.assertEqual(decoded.payload.block_number, 58239320)
        self.assertEqual(decoded.payload.completeness, "ready")

        self.assertEqual(encode_record_json(decoded), encoded)
        self.assertEqual(canonical_record_hash(record), canonical_record_hash(decoded))

    def test_state_version_with_null_applied_cursor_roundtrip(self) -> None:
        """Verify StateVersion with applied_cursor=None encodes 'applied_cursor': null and decodes to None."""
        state_ver = StateVersion(
            chain_id=4663,
            block_number=58239320,
            block_hash="0x" + "cc" * 32,
            received_at_ms=1725900010000,
            applied_cursor=None,
            complete_through_block=58239320,
            completeness="ready",
        )
        record = ContractRecord(
            schema_id="arbitrage-evidence",
            schema_version="1.0.0",
            record_type="state_version",
            run_id="run-state-002",
            data_mode=DataMode.CONFIRMED_CHAIN_HISTORY,
            provenance={"witness": "w4_state_graph"},
            payload=state_ver,
        )

        encoded = encode_record_json(record)
        self.assertIn('"applied_cursor":null', encoded)

        decoded = decode_record_json(encoded)
        self.assertIsInstance(decoded.payload, StateVersion)
        self.assertIsNone(decoded.payload.applied_cursor)
        self.assertEqual(encode_record_json(decoded), encoded)
        self.assertEqual(canonical_record_hash(record), canonical_record_hash(decoded))

    def test_reject_bad_cursor_fields_during_deserialization(self) -> None:
        """Verify malformed applied_cursor fields raise appropriate exceptions fail-closed."""
        # 1. Invalid block_hash length
        bad_cursor_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "state_version",
                "run_id": "run-bad-cur-01",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {
                    "chain_id": 4663,
                    "block_number": 100,
                    "block_hash": "0x" + "11" * 32,
                    "received_at_ms": 1000,
                    "applied_cursor": {
                        "block_hash": "0xshort",
                    },
                },
            }
        )
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(bad_cursor_json)
        self.assertIn("Invalid bytes32", str(ctx.exception))

        # 2. Negative transaction_index
        bad_idx_json = json.dumps(
            {
                "schema_id": "arbitrage-evidence",
                "schema_version": "1.0.0",
                "record_type": "state_version",
                "run_id": "run-bad-cur-02",
                "data_mode": "synthetic",
                "provenance": {},
                "payload": {
                    "chain_id": 4663,
                    "block_number": 100,
                    "block_hash": "0x" + "11" * 32,
                    "received_at_ms": 1000,
                    "applied_cursor": {
                        "block_hash": "0x" + "22" * 32,
                        "transaction_index": -1,
                    },
                },
            }
        )
        with self.assertRaises(ValueError) as ctx:
            decode_record_json(bad_idx_json)
        self.assertIn("transaction_index must be non-negative", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
