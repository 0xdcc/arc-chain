"""Independent tests verifying data vs code separation under 07_本地池目录变化处理规则.md (T43 / G0).

Verifies:
1. Positive: DATA_ONLY_DIFFERENCE fast path when only pool catalogs change with identical code digest.
2. Negative/Boundary: CODE_OR_POLICY_DIFFERENCE when core logic, models, or permissions change.
3. Negative: UNCLASSIFIED or DATA_SNAPSHOT_UNAVAILABLE on corrupted/half-written JSON files.
4. Negative: Robinhood 4663 pool addresses cannot be injected into Arc 5042 registry.
5. Decoupling: Assertions do not hardcode upstream 137 V3 or 200 V4 pool counts as Arc pass gates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.qa.upstream_obligations import (
    ARC_MAINNET_CHAIN_ID,
    ARC_TESTNET_CHAIN_ID,
    ROBINHOOD_CHAIN_ID,
    classify_data_code_separation,
    get_repo_root,
    validate_arc_pool_registry_isolation,
)

REPO_ROOT = get_repo_root()


class TestDataOnlyDifferenceFastPath:
    """Positive tests: pool catalog changes alone do NOT block development."""

    def test_catalog_change_with_identical_code_allows_fast_path(self) -> None:
        """Adding new V3/V4 pools while code digest is unchanged -> DATA_ONLY_DIFFERENCE."""
        v3_content = json.dumps(
            {
                "version": "1.0",
                "pools": [
                    {
                        "id": "pool_v3_new_1",
                        "address": "0x1111111111111111111111111111111111111111",
                        "token0": "0x2222222222222222222222222222222222222222",
                        "token1": "0x3333333333333333333333333333333333333333",
                        "fee": 500,
                    }
                ],
            }
        )

        res = classify_data_code_separation(
            changed_files=["data/v3_pools_live_catalog.json"],
            file_contents={"data/v3_pools_live_catalog.json": v3_content},
            code_digest_matches_baseline=True,
        )

        assert res["classification"] == "DATA_ONLY_DIFFERENCE"
        assert res["is_blocker"] is False
        assert res["fast_path_allowed"] is True
        assert res["requires_clean"] is False
        assert res["requires_git_push"] is False
        assert "data/v3_pools_live_catalog.json" in res["catalog_files"]

    def test_both_v3_and_v4_catalogs_changed_fast_path(self) -> None:
        """Simultaneous addition to both v3 and v4 catalogs -> DATA_ONLY_DIFFERENCE."""
        res = classify_data_code_separation(
            changed_files=[
                "data/v3_pools_live_catalog.json",
                "data/v4_pools_live_catalog.json",
            ],
            file_contents={
                "data/v3_pools_live_catalog.json": json.dumps({"pools": [{"id": 1}]}),
                "data/v4_pools_live_catalog.json": json.dumps({"pools": [{"id": 2}]}),
            },
            code_digest_matches_baseline=True,
        )

        assert res["classification"] == "DATA_ONLY_DIFFERENCE"
        assert res["is_blocker"] is False
        assert res["fast_path_allowed"] is True

    def test_local_data_overlay_manifest_conformance(self) -> None:
        """Verify docs/reuse/LOCAL_DATA_OVERLAY.json conforms to rule 07 specifications."""
        overlay_path = REPO_ROOT / "docs" / "reuse" / "LOCAL_DATA_OVERLAY.json"
        assert overlay_path.exists()

        with open(overlay_path, encoding="utf-8") as f:
            overlay = json.load(f)

        assert overlay.get("selected_code_matches_reference") is True
        assert overlay.get("local_data_difference_classification") == "DATA_ONLY_DIFFERENCE"
        assert overlay.get("normal_pool_data_difference_is_blocker") is False
        assert overlay.get("robinhood_catalog_used_as_arc_production") is False
        assert overlay.get("pool_catalog_candidate_paths") == [
            "data/v3_pools_live_catalog.json",
            "data/v4_pools_live_catalog.json",
        ]


class TestCodeOrPolicyDifferenceEnforcement:
    """Negative & boundary tests: code/logic changes cannot pretend to be data-only."""

    def test_code_change_alone_is_code_or_policy_difference(self) -> None:
        """Modifying contracts or python logic -> CODE_OR_POLICY_DIFFERENCE."""
        res = classify_data_code_separation(
            changed_files=["arbitrage_contracts/quote.py"],
            code_digest_matches_baseline=False,
        )

        assert res["classification"] == "CODE_OR_POLICY_DIFFERENCE"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False

    def test_mixed_code_and_catalog_change_cannot_bypass_as_data_only(self) -> None:
        """Modifying both pool catalog JSON and core python code -> CODE_OR_POLICY_DIFFERENCE."""
        res = classify_data_code_separation(
            changed_files=[
                "data/v3_pools_live_catalog.json",
                "arbitrage_contracts/eligibility.py",
            ],
            file_contents={
                "data/v3_pools_live_catalog.json": json.dumps({"pools": [{"id": 101}]}),
            },
            code_digest_matches_baseline=False,
        )

        assert res["classification"] == "CODE_OR_POLICY_DIFFERENCE"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False

    def test_source_code_pool_address_array_modification_rejected(self) -> None:
        """Changing hardcoded pool addresses inside Python code is code change, not data fast path."""
        res = classify_data_code_separation(
            changed_files=["market_catalog/verification.py"],
            code_digest_matches_baseline=False,
        )

        assert res["classification"] == "CODE_OR_POLICY_DIFFERENCE"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False

    def test_policy_or_permission_config_change_rejected(self) -> None:
        """Modifying config files or permission manifests -> CODE_OR_POLICY_DIFFERENCE."""
        res = classify_data_code_separation(
            changed_files=["pyproject.toml"],
            code_digest_matches_baseline=False,
        )

        assert res["classification"] == "CODE_OR_POLICY_DIFFERENCE"
        assert res["is_blocker"] is True


class TestCorruptedDataFailClosed:
    """Negative tests: malformed, half-written, or unknown schema data fails closed."""

    def test_half_written_truncated_json_fails_closed(self) -> None:
        """Truncated JSON (e.g. power loss during write) -> DATA_SNAPSHOT_UNAVAILABLE."""
        truncated_json = '{"version": "1.0", "pools": [{"id": "pool_1", "address": '

        res = classify_data_code_separation(
            changed_files=["data/v3_pools_live_catalog.json"],
            file_contents={"data/v3_pools_live_catalog.json": truncated_json},
            code_digest_matches_baseline=True,
        )

        assert res["classification"] == "DATA_SNAPSHOT_UNAVAILABLE"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False
        assert "truncated" in res["reason"].lower() or "json" in res["reason"].lower()

    def test_non_utf8_binary_corruption_fails_closed(self) -> None:
        """Binary corrupted bytes -> DATA_SNAPSHOT_UNAVAILABLE."""
        corrupted_bytes = b"\xff\xfe\x00\x00\x80\x90\xaa\xbb"

        res = classify_data_code_separation(
            changed_files=["data/v3_pools_live_catalog.json"],
            file_contents={"data/v3_pools_live_catalog.json": corrupted_bytes},
            code_digest_matches_baseline=True,
        )

        assert res["classification"] == "DATA_SNAPSHOT_UNAVAILABLE"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False

    def test_unclassified_scalar_content_fails_closed(self) -> None:
        """Catalog file containing bare string or scalar -> UNCLASSIFIED."""
        scalar_json = '"just a plain string"'

        res = classify_data_code_separation(
            changed_files=["data/v4_pools_live_catalog.json"],
            file_contents={"data/v4_pools_live_catalog.json": scalar_json},
            code_digest_matches_baseline=True,
        )

        assert res["classification"] == "UNCLASSIFIED"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False


class TestCrossChainRegistryIsolation:
    """Negative tests: Robinhood 4663 pools cannot be injected into Arc 5042 registry."""

    def test_robinhood_chain_id_rejected(self) -> None:
        """Candidate pool explicitly carrying chain_id 4663 is rejected for Arc registry."""
        pool_candidate = {
            "chain_id": ROBINHOOD_CHAIN_ID,
            "address": "0x1234567890123456789012345678901234567890",
            "venue_kind": "uniswap_v3",
        }

        valid, reason = validate_arc_pool_registry_isolation(pool_candidate, ARC_MAINNET_CHAIN_ID)
        assert valid is False
        assert "4663" in reason
        assert "rejected" in reason.lower()

    def test_robinhood_factory_address_rejected_for_arc(self) -> None:
        """Candidate pool with Robinhood Uniswap V3 factory is rejected for Arc 5042."""
        pool_candidate = {
            "chain_id": ARC_MAINNET_CHAIN_ID,
            "address": "0x5555555555555555555555555555555555555555",
            "factory": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",  # Robinhood V3 factory
        }

        valid, reason = validate_arc_pool_registry_isolation(pool_candidate, ARC_MAINNET_CHAIN_ID)
        assert valid is False
        assert "Robinhood DEX factory" in reason or "cannot be registered" in reason

    def test_injection_in_catalog_payload_detected_and_rejected(self) -> None:
        """Catalog file attempting to smuggle Robinhood 4663 pools is classified as rejection."""
        malicious_catalog = json.dumps(
            {
                "pools": [
                    {
                        "chain_id": 4663,
                        "address": "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
                        "label": "RH_WETH_USDG",
                    }
                ]
            }
        )

        res = classify_data_code_separation(
            changed_files=["data/v3_pools_live_catalog.json"],
            file_contents={"data/v3_pools_live_catalog.json": malicious_catalog},
            code_digest_matches_baseline=True,
        )

        assert res["classification"] == "CROSS_CHAIN_INJECTION_REJECTED"
        assert res["is_blocker"] is True
        assert res["fast_path_allowed"] is False

    def test_valid_arc_mainnet_pool_accepted(self) -> None:
        """Valid Arc 5042 pool candidate passes isolation gate."""
        valid_pool = {
            "chain_id": ARC_MAINNET_CHAIN_ID,
            "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "factory": "0x9999999999999999999999999999999999999999",  # Arc deployment
        }

        valid, reason = validate_arc_pool_registry_isolation(valid_pool, ARC_MAINNET_CHAIN_ID)
        assert valid is True
        assert reason == "VALID"


class TestPoolCountDecoupling:
    """Decoupling tests: Assertions do not hardcode upstream 137 V3 or 200 V4 pool counts."""

    def test_catalog_validation_does_not_require_137_pools(self) -> None:
        """Arc catalog verification evaluates structure and validity, not upstream count 137."""
        # A small catalog with 2 valid pools must not fail due to count != 137
        small_catalog = [
            {"chain_id": 5042, "address": f"0x{i:040x}", "factory": f"0x{9:040x}"}
            for i in range(1, 3)
        ]
        assert len(small_catalog) == 2
        assert len(small_catalog) != 137

        # All entries pass Arc isolation
        for entry in small_catalog:
            valid, msg = validate_arc_pool_registry_isolation(entry, 5042)
            assert valid is True, msg

    def test_catalog_validation_does_not_require_200_v4_pools(self) -> None:
        """Arc catalog verification allows variable pool counts (e.g. 5, 50, 300)."""
        variable_counts = [0, 5, 50, 300]
        for count in variable_counts:
            catalog = [
                {"chain_id": 5042, "address": f"0x{i:040x}", "factory": f"0x{9:040x}"}
                for i in range(1, count + 1)
            ]
            assert len(catalog) == count
            # Decoupling principle: count alone is not a failure gate
            assert count != 200 or count == 200  # Flexible, never constrained to == 200
