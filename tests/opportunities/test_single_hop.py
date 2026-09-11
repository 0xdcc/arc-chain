"""Regression tests for A36-A41 independent single-hop quoting."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from arbitrage_contracts.eligibility import AssetEligibility, PoolCapability
from arbitrage_contracts.identity import Amount, AssetRef, PoolDescriptor, PoolKey, TokenKey
from arbitrage_contracts.quote import FeeModel, GasEvidenceKind, QuoteStatus
from arbitrage_contracts.serialization import ContractRecord, encode_record_json
from arbitrage_contracts.state import StateVersion
from opportunities.input_gate import load_w1_bootstrap_catalog
from opportunities.single_hop import (
    SingleHopQuoteAdapter,
    SingleHopQuoteEvidence,
    SingleHopQuoteInputError,
    SingleHopQuoteRequest,
    encode_single_hop_calldata,
)
from opportunities.single_hop_lifecycle import LifecycleInputError, run_single_hop_lifecycle

CHAIN_ID = 4663
BLOCK = 0x378A957
TOKEN_A = "0x00000000000000000000000000000000000000aa"
TOKEN_B = "0x00000000000000000000000000000000000000bb"
STATE = StateVersion(
    CHAIN_ID,
    BLOCK,
    "0x" + "1" * 64,
    received_at_ms=10,
    block_domain="l2",
    complete_through_block=BLOCK,
    completeness="ready",
)


def _asset(address: str) -> Any:
    return AssetRef.erc20(TokenKey(CHAIN_ID, address))


def _pool(protocol: str = "uniswap_v3") -> PoolDescriptor:
    pool_key = PoolKey(
        CHAIN_ID,
        protocol,
        "factory" if protocol == "uniswap_v3" else "manager",
        "0x0000000000000000000000000000000000000001",
        "address" if protocol == "uniswap_v3" else "bytes32",
        "0x0000000000000000000000000000000000000002"
        if protocol == "uniswap_v3"
        else "0x" + "2" * 64,
    )
    return PoolDescriptor(
        pool_key,
        _asset(TOKEN_A),
        _asset(TOKEN_B),
        FeeModel.static(3000),
        tick_spacing=None if protocol == "uniswap_v4" else 60,
        hooks=None if protocol == "uniswap_v4" else "0x0000000000000000000000000000000000000003",
    )


def _v4_pool(complete: bool = True) -> PoolDescriptor:
    pool_key = PoolKey(
        CHAIN_ID,
        "uniswap_v4",
        "manager",
        "0x0000000000000000000000000000000000000001",
        "bytes32",
        "0x" + "2" * 64,
    )
    return PoolDescriptor(
        pool_key,
        _asset(TOKEN_A),
        _asset(TOKEN_B),
        FeeModel.static(3000),
        tick_spacing=60 if complete else None,
        hooks="0x0000000000000000000000000000000000000003" if complete else None,
    )


def _eligibility(address: str, **overrides: object) -> AssetEligibility:
    values: dict[str, Any] = {
        "decimals_status": "verified",
        "decimals_evidence_ref": "test:decimals",
        "review_status": "approved",
        "reviewer_ref": "test:reviewer",
        "reviewed_at_ms": 1,
        "evidence_refs": ("test:asset",),
        "subject_scope": "public",
    }
    values.update(overrides)
    return AssetEligibility(
        _asset(address),
        **values,
    )


def _catalog(
    temporary_directory: Path,
    *,
    review_a: str = "approved",
    decimals_a: str = "verified",
    review_b: str = "approved",
    decimals_b: str = "verified",
) -> Path:
    descriptor = _pool()
    capability = PoolCapability(descriptor.key, can_quote="supported")
    records: list[Any] = []
    for address, review, decimals, provenance in (
        (TOKEN_A, review_a, decimals_a, {"decimals": 18}),
        (TOKEN_B, review_b, decimals_b, {"decimals": 18}),
    ):
        records.append(
            ContractRecord(
                "arbitrage-evidence",
                "1.0.0",
                "asset_eligibility",
                "test:catalog",
                "synthetic",
                provenance,
                _eligibility(address, review_status=review, decimals_status=decimals),
            )
        )
    records.append(
        ContractRecord(
            "arbitrage-evidence",
            "1.0.0",
            "pool_descriptor",
            "test:catalog",
            "synthetic",
            {},
            descriptor,
        )
    )
    records.append(
        ContractRecord(
            "arbitrage-evidence",
            "1.0.0",
            "pool_capability",
            "test:catalog",
            "synthetic",
            {},
            capability,
        )
    )
    by_type: dict[str, list[str]] = {
        "eligibility": [],
        "pools": [],
        "capabilities": [],
    }
    hashes: dict[str, str] = {}
    for record in records:
        partition_name = {
            "asset_eligibility": "eligibility",
            "pool_descriptor": "pools",
            "pool_capability": "capabilities",
        }[record.record_type]
        line = encode_record_json(record) + "\n"
        by_type[partition_name].append(line)
    temporary_directory.mkdir(exist_ok=True)
    for name, lines in by_type.items():
        (temporary_directory / f"{name}.jsonl").write_text("".join(lines), encoding="utf-8")
        hashes[name] = hashlib.sha256(
            (temporary_directory / f"{name}.jsonl").read_bytes()
        ).hexdigest()
    manifest = {
        "header": {
            "catalog_schema": "w1-catalog-manifest",
            "version": "1.0.0-draft",
            "data_mode": "synthetic",
            "registry_revision": "registry-v1",
        },
        "partitions": {
            name: {
                "path": f"{name}.jsonl",
                "sha256": hashes[name],
                "rows": len(by_type[name]),
            }
            for name in by_type
        },
    }
    (temporary_directory / "catalog_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    return temporary_directory


def _request() -> SingleHopQuoteRequest:
    return SingleHopQuoteRequest(
        quoter_v3="0x0000000000000000000000000000000000000011",
        quoter_v4="0x0000000000000000000000000000000000000012",
        data_mode="synthetic",
        actor_scope="synthetic",
        source_refs=("test:single-hop",),
    )


def _rpc(responses: list[dict[str, Any]] | None = None) -> Any:
    class Rpc:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.responses = responses or []

        def call(
            self,
            method: str,
            params: list[Any] | tuple[Any, ...] | None = None,
            block_identifier: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {"method": method, "params": params, "block_identifier": block_identifier}
            )
            if self.responses:
                return self.responses.pop(0)
            encoded = (1000000000000000000).to_bytes(32, "big") + (0).to_bytes(32, "big")
            encoded += (0).to_bytes(32, "big") + (50000).to_bytes(32, "big")
            return {"result": "0x" + encoded.hex()}

    return Rpc()


def test_a36_quote_envelope_direction_and_failure_invariants() -> None:
    descriptor = _pool()
    adapter = SingleHopQuoteAdapter(_rpc(), _request())
    quoted = adapter.quote(
        descriptor,
        "zero_for_one",
        Amount(_asset(TOKEN_A), 10**18, 18),
        STATE,
    )
    assert quoted.evidence.status == QuoteStatus.QUOTED
    assert quoted.evidence.amount_out is not None
    assert quoted.evidence.amount_out.asset_ref == _asset(TOKEN_B)
    assert quoted.evidence.gas_evidence is not None
    assert quoted.evidence.gas_evidence.gas_kind == GasEvidenceKind.QUOTER_ESTIMATE
    failed = SingleHopQuoteEvidence(
        quote_id="failed",
        pool_descriptor=descriptor,
        direction="zero_for_one",
        asset_in=_asset(TOKEN_A),
        asset_out=_asset(TOKEN_B),
        amount_in=Amount(_asset(TOKEN_A), 10**18, 18),
        status=QuoteStatus.CONTRACT_REVERT,
    )
    assert failed.amount_out is None
    with pytest.raises(ValueError, match="direction"):
        SingleHopQuoteEvidence(
            quote_id="bad-direction",
            pool_descriptor=descriptor,
            direction="one_for_zero",
            asset_in=_asset(TOKEN_A),
            asset_out=_asset(TOKEN_B),
            amount_in=Amount(_asset(TOKEN_A), 10**18, 18),
            amount_out=Amount(_asset(TOKEN_B), 1, 18),
        )


def test_a37_both_directions_are_independent_fixed_block_rpc_calls() -> None:
    descriptor = _pool()
    rpc = _rpc()
    adapter = SingleHopQuoteAdapter(rpc, _request())
    first = adapter.quote(
        descriptor,
        "zero_for_one",
        Amount(_asset(TOKEN_A), 10**18, 18),
        STATE,
    )
    second = adapter.quote(
        descriptor,
        "one_for_zero",
        Amount(_asset(TOKEN_B), 10**18, 18),
        STATE,
    )
    assert first.evidence.status == QuoteStatus.QUOTED
    assert second.evidence.status == QuoteStatus.QUOTED
    assert first.evidence.quote_id != second.evidence.quote_id
    assert len(rpc.calls) == 2
    assert all(call["params"][1] == hex(BLOCK) for call in rpc.calls)
    assert all(call["block_identifier"] == hex(BLOCK) for call in rpc.calls)
    assert rpc.calls[0]["params"][0]["data"] != rpc.calls[1]["params"][0]["data"]

    independent_rpc = _rpc(
        [
            {"error": {"code": 3, "data": "0x08c379a0", "message": "insufficient liquidity"}},
        ]
    )
    other_adapter = SingleHopQuoteAdapter(independent_rpc, _request())
    zero_failed = other_adapter.quote(
        descriptor,
        "zero_for_one",
        Amount(_asset(TOKEN_A), 10**18, 18),
        STATE,
    )
    one_succeeded = other_adapter.quote(
        descriptor,
        "one_for_zero",
        Amount(_asset(TOKEN_B), 10**18, 18),
        STATE,
    )
    assert zero_failed.evidence.status == QuoteStatus.CONTRACT_REVERT
    assert one_succeeded.evidence.status == QuoteStatus.QUOTED


def test_a38_v3_and_v4_encoding_and_incomplete_v4() -> None:
    descriptor = _pool()
    calldata = encode_single_hop_calldata(descriptor, "zero_for_one", 10**18)
    assert calldata.startswith("0xc6a5026a")
    assert TOKEN_A[2:].lower() in calldata.lower()
    assert TOKEN_B[2:].lower() in calldata.lower()

    complete = _v4_pool()
    zero = encode_single_hop_calldata(complete, "zero_for_one", 10**18)
    one = encode_single_hop_calldata(complete, "one_for_zero", 10**18)
    assert zero.startswith("0xaa9d21cb")
    assert one.startswith("0xaa9d21cb")
    assert zero != one
    assert TOKEN_A[2:].lower() in zero.lower()
    assert TOKEN_A[2:].lower() in one.lower()
    assert TOKEN_B[2:].lower() in one.lower()

    incomplete = _v4_pool(complete=False)
    with pytest.raises(SingleHopQuoteInputError, match="lacks hooks"):
        encode_single_hop_calldata(incomplete, "zero_for_one", 10**18)


@pytest.mark.parametrize(
    ("review_a", "decimals_a", "expected"),
    [
        ("pending_review", "verified", "asset_review_pending_review"),
        ("approved", "unknown", "asset_decimals_unverified"),
    ],
)
def test_a39_w1_bootstrap_catalog_rejects_unqualified_assets(
    tmp_path: Path,
    review_a: str,
    decimals_a: str,
    expected: str,
) -> None:
    catalog_dir = _catalog(tmp_path, review_a=review_a, decimals_a=decimals_a)
    catalog = load_w1_bootstrap_catalog(catalog_dir)
    assert catalog.descriptors == {}
    pool_key = _pool().key
    asset_ref = _asset(TOKEN_A)
    assert expected in catalog.rejection_reasons[f"asset:{asset_ref}"]
    assert f"pool:{pool_key}" in catalog.rejection_reasons


def test_a39_qualified_w1_bootstrap_catalog_is_admitted(tmp_path: Path) -> None:
    catalog = load_w1_bootstrap_catalog(_catalog(tmp_path))
    assert len(catalog.descriptors) == 1
    assert len(catalog.pools) == 1


def test_a40_bidirectional_curve_and_capability_promotion(tmp_path: Path) -> None:
    descriptor = _pool()
    output_root = tmp_path / "output"
    output_root.mkdir()
    result = run_single_hop_lifecycle(
        {descriptor.key: descriptor},
        (10**18,),
        STATE,
        SingleHopQuoteAdapter(_rpc(), _request()),
        output_root,
    )
    curve = result[descriptor.key]
    assert curve.capability.can_quote == "supported"
    assert len(curve.capability.evidence_refs) == 2
    output = (
        output_root
        / f"{CHAIN_ID}_{descriptor.key.canonical_pool_id}"
        / "quote_curves.jsonl"
    )
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 2
    assert {row["direction"] for row in rows} == {"zero_for_one", "one_for_zero"}
    assert all("delta" not in row and "profit" not in row for row in rows)


def test_a40_one_direction_failure_keeps_capability_unsupported(tmp_path: Path) -> None:
    descriptor = _pool()
    output_root = tmp_path / "output"
    output_root.mkdir()
    rpc = _rpc([{"error": {"code": 3, "data": "0x08c379a0"}}])
    result = run_single_hop_lifecycle(
        {descriptor.key: descriptor},
        (10**18,),
        STATE,
        SingleHopQuoteAdapter(rpc, _request()),
        output_root,
    )
    curve = result[descriptor.key]
    assert curve.capability.can_quote == "unsupported"
    assert curve.capability.reasons


def test_a41_sabotage_block_tag_turns_red() -> None:
    descriptor = _pool()
    rpc = _rpc()
    adapter = SingleHopQuoteAdapter(rpc, _request())
    adapter.quote(descriptor, "zero_for_one", Amount(_asset(TOKEN_A), 10**18, 18), STATE)
    tampered_state = StateVersion(
        CHAIN_ID,
        0,
        "0x" + "2" * 64,
        0,
        block_domain="l2",
        complete_through_block=0,
        completeness="ready",
    )
    with pytest.raises((TypeError, ValueError, AttributeError)):
        adapter.quote(
            descriptor,
            "zero_for_one",
            Amount(_asset(TOKEN_A), 10**18, 18),
            tampered_state,
        )


def test_a41_sabotage_one_for_zero_pool_key_reversal_turns_red() -> None:
    descriptor = _v4_pool()
    correct = encode_single_hop_calldata(descriptor, "one_for_zero", 10**18)
    sabotaged = encode_single_hop_calldata(descriptor, "zero_for_one", 10**18)
    assert correct != sabotaged
    assert TOKEN_A[2:].lower() in correct.lower()
    assert TOKEN_B[2:].lower() in correct[:166].lower()


def test_a41_sabotage_single_direction_promotion_turns_red(tmp_path: Path) -> None:
    descriptor = _pool()
    rpc = _rpc([{"error": {"code": 3, "data": "0x08c379a0"}}])
    result = run_single_hop_lifecycle(
        {descriptor.key: descriptor},
        (10**18,),
        STATE,
        SingleHopQuoteAdapter(rpc, _request()),
        tmp_path,
    )
    assert result[descriptor.key].capability.can_quote != "supported"


def test_cli_main_is_fail_closed(tmp_path: Path) -> None:
    import apps.probe_single_hop as probe

    catalog = _catalog(tmp_path / "catalog")
    sys.argv = [
        "probe",
        "--catalog",
        str(catalog),
        "--state-json",
        json.dumps(
            {
                "block_domain": "l2",
                "block_hash": "0x" + "1" * 64,
                "block_number": BLOCK,
                "chain_id": CHAIN_ID,
                "completeness": "ready",
                "complete_through_block": BLOCK,
                "received_at_ms": 0,
            }
        ),
        "--output-root",
        str(tmp_path / "output"),
    ]
    assert probe.main() == 0
    assert catalog.exists()
    with pytest.raises(LifecycleInputError):
        run_single_hop_lifecycle(
            {}, (), STATE, SingleHopQuoteAdapter(_rpc(), _request()), Path("/tmp/unused")
        )
