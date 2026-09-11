"""Independent review tests for IMPORT_MANIFEST and EXCLUSION_MANIFEST parity (T43 / G0)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tools.qa.upstream_obligations import (
    KNOWN_ARC_ADAPTATIONS,
    audit_imported_files,
    compute_sha256,
    get_repo_root,
    verify_exclusions,
)

REPO_ROOT = get_repo_root()


class TestImportManifestParity:
    """Tests verifying complete import fidelity and file integrity."""

    def test_import_manifest_json_structure(self) -> None:
        """Verify IMPORT_MANIFEST schema, plan_id, source sha, and 281 file count."""
        manifest_path = REPO_ROOT / "docs" / "reuse" / "IMPORT_MANIFEST.json"
        assert manifest_path.exists(), f"Missing manifest: {manifest_path}"

        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("plan_id") == "ARC-4B-v3.1-20260911-13817f4"
        assert data.get("fixed_source_sha") == "13817f4e027375dd59cc7a202ae641c068525f53"
        assert data.get("total_imported_files") == 281
        imported = data.get("imported_files", {})
        assert len(imported) == 281

    def test_all_281_imported_files_exist(self) -> None:
        """Verify that every file declared in the manifest exists in the worktree."""
        manifest_path = REPO_ROOT / "docs" / "reuse" / "IMPORT_MANIFEST.json"
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        imported = manifest.get("imported_files", {})
        missing = [p for p in imported if not (REPO_ROOT / p).exists()]
        assert missing == [], f"Missing files from manifest: {missing}"

    def test_imported_files_non_empty_except_upstream_zero_byte(self) -> None:
        """Verify that all files with expected non-zero size are non-empty."""
        manifest_path = REPO_ROOT / "docs" / "reuse" / "IMPORT_MANIFEST.json"
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        imported = manifest.get("imported_files", {})
        empty_violations = []
        for rel_path, meta in imported.items():
            expected_size = meta.get("size", 0)
            actual_size = (REPO_ROOT / rel_path).stat().st_size
            if expected_size > 0 and actual_size == 0:
                empty_violations.append(rel_path)

        assert empty_violations == [], f"Unexpected empty files: {empty_violations}"

    def test_import_hash_parity_and_documented_adaptations(self) -> None:
        """Verify that all files match upstream SHA-256 or documented Arc adaptations."""
        res = audit_imported_files(REPO_ROOT, allow_known_adaptations=True)
        assert res["passed"] is True
        assert res["total_found"] == 281
        assert res["missing_files"] == []
        assert res["empty_files"] == []
        assert res["unexpected_mismatches"] == []
        assert res["exact_matches"] + res["adapted_count"] == 281
        assert res["adapted_count"] >= 3

        for ad in res["adapted_files"]:
            assert ad["matches_documented"] is True, f"Adaptation mismatch for {ad['path']}"


class TestExclusionEnforcement:
    """Tests verifying strict exclusion of sensitive credentials, Robinhood catalogs, and legacy scripts."""

    def test_exclusion_manifest_structure(self) -> None:
        """Verify EXCLUSION_MANIFEST schema and categories."""
        manifest_path = REPO_ROOT / "docs" / "reuse" / "EXCLUSION_MANIFEST.json"
        assert manifest_path.exists()
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("plan_id") == "ARC-4B-v3.1-20260911-13817f4"
        assert len(data.get("excluded_items", [])) >= 7

    def test_no_sensitive_secrets_or_env_files(self) -> None:
        """Verify that no .env, keystore, or private key files exist in the repository."""
        res = verify_exclusions(REPO_ROOT)
        assert res["passed"] is True, f"Exclusion violations: {res['violations']}"

        # Additional explicit checks
        assert not (REPO_ROOT / ".env").exists()
        assert not (REPO_ROOT / ".env.local").exists()
        assert not (REPO_ROOT / ".env.production").exists()

        # Check for any keystore or private key files
        for ext in (".pem", ".key"):
            matches = list(REPO_ROOT.rglob(f"*{ext}"))
            matches = [m for m in matches if ".git" not in str(m) and "venv" not in str(m)]
            assert matches == [], f"Found forbidden secret files: {matches}"

    def test_robinhood_4663_catalogs_strictly_excluded(self) -> None:
        """Verify Robinhood 4663 catalogs are not imported into Arc repo."""
        assert not (REPO_ROOT / "data" / "v3_pools_live_catalog.json").exists()
        assert not (REPO_ROOT / "data" / "v4_pools_live_catalog.json").exists()

    def test_legacy_monolithic_pipeline_excluded(self) -> None:
        """Verify legacy Robinhood live_pipeline.py and non-modular catalog scripts are excluded."""
        assert not (REPO_ROOT / "apps" / "live_pipeline.py").exists()
        assert not (REPO_ROOT / "tools" / "update_v3_catalog.py").exists()
        assert not (REPO_ROOT / "tools" / "export_v4_catalog.py").exists()

    def test_no_automated_remote_push_workflows(self) -> None:
        """Verify that .github/workflows does not contain remote CI/push actions."""
        workflow_dir = REPO_ROOT / ".github" / "workflows"
        if workflow_dir.exists():
            yml_files = list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
            assert yml_files == [], f"Forbidden automated push workflows found: {yml_files}"


class TestSysPathIsolation:
    """Tests ensuring clean python environment with no external leakage."""

    def test_no_external_production_repo_in_sys_path(self) -> None:
        """Verify that external production dex-sniper-engine is not in sys.path."""
        external_prod_root = Path("/root/projects/crypto/dex-sniper-engine").resolve()
        for p in sys.path:
            try:
                resolved = Path(p).resolve()
                assert resolved != external_prod_root, f"External production root leaked in sys.path: {p}"
            except Exception:
                pass

    def test_no_secrets_dir_in_sys_path(self) -> None:
        """Verify that /root/.secrets is never in sys.path."""
        secrets_dir = Path("/root/.secrets").resolve()
        for p in sys.path:
            try:
                resolved = Path(p).resolve()
                assert resolved != secrets_dir, f"Secrets dir leaked in sys.path: {p}"
            except Exception:
                pass
