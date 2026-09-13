"""Independent tests for legacy test archive migration registry and fail-closed security gates (TASK-LEGACY-V4-ARCHIVE-R1)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tools.qa.upstream_obligations import (
    KNOWN_ARC_ADAPTATIONS,
    LEGACY_TEST_MIGRATION_REGISTRY,
    audit_imported_files,
    compute_sha256,
    get_repo_root,
    resolve_imported_file_path,
    validate_legacy_migration_registry,
)

REPO_ROOT = get_repo_root()
APPROVED_LEGACY_FILE_V4 = "tests/test_v4_poolkey.py"
ARCHIVED_DEST_PATH_V4 = "docs/legacy_tests/robinhood/test_v4_poolkey.py.txt"
AUDITED_ARCHIVE_SHA_V4 = "96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a"
UPSTREAM_FROZEN_SHA_V4 = "1245821886735df5fabcba65ff37158c92d3ff8260d1a6b627b5ef56c27f46b7"

APPROVED_LEGACY_FILE_RPC = "tests/test_rpc_policy.py"
ARCHIVED_DEST_PATH_RPC = "docs/legacy_tests/robinhood/test_rpc_policy.py.txt"
AUDITED_ARCHIVE_SHA_RPC = "9540b6388e965768d36cd80098ec8a48987b027aa01a8d0d395c4c8cc92572d5"
UPSTREAM_FROZEN_SHA_RPC = "20722798985cc87ad9c2b3e656fe14bba5ce66834b5307bb9f54e49366f71ed9"

# Backward compatibility alias for single-file V4 references in existing tests
APPROVED_LEGACY_FILE = APPROVED_LEGACY_FILE_V4
ARCHIVED_DEST_PATH = ARCHIVED_DEST_PATH_V4
AUDITED_ARCHIVE_SHA = AUDITED_ARCHIVE_SHA_V4
UPSTREAM_FROZEN_SHA = UPSTREAM_FROZEN_SHA_V4


class TestLegacyArchiveRegistry:
    """Rigorous verification of single-file archive migration and safety barriers."""

    def test_registry_schema_and_single_entry_immutability(self) -> None:
        """Verify registry contains strictly 2 approved entries and all mandatory schema fields."""
        assert len(LEGACY_TEST_MIGRATION_REGISTRY) == 2, (
            f"Registry must contain exactly 2 approved entries, found {len(LEGACY_TEST_MIGRATION_REGISTRY)}"
        )
        assert set(LEGACY_TEST_MIGRATION_REGISTRY.keys()) == {
            APPROVED_LEGACY_FILE_V4,
            APPROVED_LEGACY_FILE_RPC,
        }

        # Verify V4 poolkey entry
        entry_v4 = LEGACY_TEST_MIGRATION_REGISTRY[APPROVED_LEGACY_FILE_V4]
        assert entry_v4["original_path"] == APPROVED_LEGACY_FILE_V4
        assert entry_v4["archived_path"] == ARCHIVED_DEST_PATH_V4
        assert entry_v4["upstream_original_sha256"] == UPSTREAM_FROZEN_SHA_V4
        assert entry_v4["expected_archived_sha256"] == AUDITED_ARCHIVE_SHA_V4
        assert entry_v4["authorization_ticket"] == "TASK-V4-ARCHIVE-READINESS"
        assert entry_v4["archive_policy"] == "USER_APPROVED_LEGACY_P1"
        assert entry_v4["review_evidence"] == "/tmp/arc-v4-archive-readiness/RESULT.md"
        assert entry_v4["disposition_mapping"] == "/tmp/arc-legacy-v4-disposition/mapping.json"
        successor_suites_v4 = entry_v4["successor_suites"]
        assert len(successor_suites_v4) == 4
        expected_suites_v4 = [
            "tests/arc_v3/independent/test_legacy_v4_obligations.py",
            "tests/arc_v3/independent/test_legacy_v4_metadata.py",
            "tests/arc_v3/independent/test_legacy_v4_plan_metadata.py",
            "tests/arc_v3/independent/test_v4_catalog_poolid_integrity.py",
        ]
        assert successor_suites_v4 == expected_suites_v4
        for suite in expected_suites_v4:
            assert (REPO_ROOT / suite).is_file(), f"Successor suite missing: {suite}"

        # Verify RPC policy entry
        entry_rpc = LEGACY_TEST_MIGRATION_REGISTRY[APPROVED_LEGACY_FILE_RPC]
        assert entry_rpc["original_path"] == APPROVED_LEGACY_FILE_RPC
        assert entry_rpc["archived_path"] == ARCHIVED_DEST_PATH_RPC
        assert entry_rpc["upstream_original_sha256"] == UPSTREAM_FROZEN_SHA_RPC
        assert entry_rpc["expected_archived_sha256"] == AUDITED_ARCHIVE_SHA_RPC
        assert entry_rpc["authorization_ticket"] == "TASK-RPC-POLICY-DISPOSITION"
        assert entry_rpc["archive_policy"] == "USER_APPROVED_LEGACY_P1"
        assert entry_rpc["review_evidence"] == "/tmp/arc-rpc-policy-disposition-review/REVIEW.md"
        assert entry_rpc["disposition_mapping"] == "/tmp/arc-rpc-policy-disposition-review/rectified_mapping.json"
        successor_suites_rpc = entry_rpc["successor_suites"]
        assert len(successor_suites_rpc) == 3
        expected_suites_rpc = [
            "tests/arc_v3/independent/test_readonly_boundary.py",
            "tests/arc_v3/ingest/test_profile_transport.py",
            "tests/arc_v3/independent/test_http_batch_contract.py",
        ]
        assert successor_suites_rpc == expected_suites_rpc
        for suite in expected_suites_rpc:
            assert (REPO_ROOT / suite).is_file(), f"Successor suite missing: {suite}"

    def test_normal_mapping_resolution_and_byte_preservation(self) -> None:
        """Verify normal path resolution maps to archive destination with 100% byte fidelity for both entries."""
        for orig, arch_path, audited_sha in [
            (APPROVED_LEGACY_FILE_V4, ARCHIVED_DEST_PATH_V4, AUDITED_ARCHIVE_SHA_V4),
            (APPROVED_LEGACY_FILE_RPC, ARCHIVED_DEST_PATH_RPC, AUDITED_ARCHIVE_SHA_RPC),
        ]:
            # 1. Original file must be removed from tests/
            assert not (REPO_ROOT / orig).exists(), (
                f"Original file {orig} must not exist in tests/ tree"
            )

            # 2. Resolved path must point to exact archive file
            resolved = resolve_imported_file_path(orig, REPO_ROOT)
            expected_target = REPO_ROOT / arch_path
            assert resolved == expected_target
            assert resolved.is_file(), f"Archived destination file missing: {resolved}"
            assert not resolved.is_symlink(), f"Archived destination must not be a symlink: {resolved}"
            assert resolved.stat().st_nlink == 1, f"Archived destination must not have hard links: {resolved}"

            # 3. Byte SHA-256 matches audited hash
            actual_sha = compute_sha256(resolved)
            assert actual_sha == audited_sha, (
                f"SHA mismatch for archived file {orig}: {actual_sha} != {audited_sha}"
            )

            # 4. Known adaptations chain verification
            assert orig in KNOWN_ARC_ADAPTATIONS
            adaptation = KNOWN_ARC_ADAPTATIONS[orig]
            assert adaptation["expected_sha256"] == audited_sha

    def test_full_281_obligations_retained_in_audit(self) -> None:
        """Verify full audit retains all 281 expected obligations without denominator reduction."""
        res = audit_imported_files(REPO_ROOT, allow_known_adaptations=True)
        assert res["passed"] is True
        assert res["total_expected"] == 281
        assert res["total_found"] == 281
        assert res["missing_files"] == []
        assert res["empty_files"] == []
        assert res["unexpected_mismatches"] == []
        assert res["exact_matches"] + res["adapted_count"] == 281

        # Confirm tests/test_v4_poolkey.py is accounted for in adapted_files
        v4_adapted = [ad for ad in res["adapted_files"] if ad["path"] == APPROVED_LEGACY_FILE]
        assert len(v4_adapted) == 1
        assert v4_adapted[0]["actual_sha"] == AUDITED_ARCHIVE_SHA
        assert v4_adapted[0]["expected_upstream_sha"] == UPSTREAM_FROZEN_SHA
        assert v4_adapted[0]["matches_documented"] is True

        # Confirm tests/test_rpc_policy.py is accounted for in adapted_files
        rpc_adapted = [ad for ad in res["adapted_files"] if ad["path"] == APPROVED_LEGACY_FILE_RPC]
        assert len(rpc_adapted) == 1
        assert rpc_adapted[0]["actual_sha"] == AUDITED_ARCHIVE_SHA_RPC
        assert rpc_adapted[0]["expected_upstream_sha"] == UPSTREAM_FROZEN_SHA_RPC
        assert rpc_adapted[0]["matches_documented"] is True

    def test_dual_copy_drift_rejected_fail_closed(self) -> None:
        """Verify fail-closed rejection if both original path and archived file exist simultaneously."""
        with tempfile.TemporaryDirectory(prefix="arc-dual-drift-") as tmp:
            tmp_root = Path(tmp)
            orig = tmp_root / APPROVED_LEGACY_FILE
            arch = tmp_root / ARCHIVED_DEST_PATH
            orig.parent.mkdir(parents=True, exist_ok=True)
            arch.parent.mkdir(parents=True, exist_ok=True)
            orig.write_bytes(b"# legacy original content\n")
            arch.write_bytes(b"# archived content\n")

            with pytest.raises(ValueError, match="Dual-copy drift detected"):
                resolve_imported_file_path(APPROVED_LEGACY_FILE, tmp_root)

    def test_target_tampering_rejected_fail_closed(self) -> None:
        """Verify audit fails closed when archived file contents are tampered."""
        with tempfile.TemporaryDirectory(prefix="arc-tamper-") as tmp:
            tmp_root = Path(tmp)
            manifest_dir = tmp_root / "docs" / "reuse"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "docs" / "reuse" / "IMPORT_MANIFEST.json", manifest_dir)

            # Stage tampered archived file
            arch = tmp_root / ARCHIVED_DEST_PATH
            arch.parent.mkdir(parents=True, exist_ok=True)
            arch.write_bytes(b"# Tampered content injected into archive\n")

            res = audit_imported_files(tmp_root, allow_known_adaptations=True)
            assert res["passed"] is False
            mismatches = [m for m in res["unexpected_mismatches"] if m["path"] == APPROVED_LEGACY_FILE]
            adapted = [a for a in res["adapted_files"] if a["path"] == APPROVED_LEGACY_FILE]
            # Must either be in unexpected_mismatches or adapted with matches_documented False
            if adapted:
                assert adapted[0]["matches_documented"] is False
            else:
                assert len(mismatches) == 1

    def test_target_missing_fail_closed(self) -> None:
        """Verify fail-closed reporting when archived file is missing."""
        with tempfile.TemporaryDirectory(prefix="arc-missing-") as tmp:
            tmp_root = Path(tmp)
            manifest_dir = tmp_root / "docs" / "reuse"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "docs" / "reuse" / "IMPORT_MANIFEST.json", manifest_dir)

            # Strict resolution raises FileNotFoundError
            with pytest.raises(FileNotFoundError, match="Registered archived file missing"):
                resolve_imported_file_path(APPROVED_LEGACY_FILE, tmp_root, strict=True)

            # Audit flags it in missing_files
            res = audit_imported_files(tmp_root, allow_known_adaptations=True)
            assert res["passed"] is False
            assert APPROVED_LEGACY_FILE in res["missing_files"]

    def test_path_traversal_and_escapes_rejected_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify path traversal (..) and invalid parent directory are rejected fail-closed."""
        bad_entry: dict[str, Any] = dict(LEGACY_TEST_MIGRATION_REGISTRY[APPROVED_LEGACY_FILE])
        bad_entry["archived_path"] = "docs/legacy_tests/robinhood/../../etc/passwd"

        monkeypatch.setitem(LEGACY_TEST_MIGRATION_REGISTRY, APPROVED_LEGACY_FILE, bad_entry)

        with pytest.raises(ValueError, match="Path traversal"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, REPO_ROOT)

    def test_absolute_path_rejected_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify absolute destination paths are rejected fail-closed."""
        bad_entry: dict[str, Any] = dict(LEGACY_TEST_MIGRATION_REGISTRY[APPROVED_LEGACY_FILE])
        bad_entry["archived_path"] = "/tmp/test_v4_poolkey.py.txt"

        monkeypatch.setitem(LEGACY_TEST_MIGRATION_REGISTRY, APPROVED_LEGACY_FILE, bad_entry)

        with pytest.raises(ValueError, match="Absolute path forbidden"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, REPO_ROOT)

    def test_outside_directory_confinement_rejected_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify destination not within docs/legacy_tests/robinhood/ is rejected fail-closed."""
        bad_entry: dict[str, Any] = dict(LEGACY_TEST_MIGRATION_REGISTRY[APPROVED_LEGACY_FILE])
        bad_entry["archived_path"] = "docs/other_tests/test_v4_poolkey.py.txt"

        monkeypatch.setitem(LEGACY_TEST_MIGRATION_REGISTRY, APPROVED_LEGACY_FILE, bad_entry)

        with pytest.raises(ValueError, match="strictly within 'docs/legacy_tests/robinhood/'"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, REPO_ROOT)

    def test_symlink_archived_file_rejected_fail_closed(self) -> None:
        """Verify symlinked archived files are rejected fail-closed."""
        with tempfile.TemporaryDirectory(prefix="arc-symlink-") as tmp:
            tmp_root = Path(tmp)
            real_file = tmp_root / "real.txt"
            real_file.write_bytes(b"content")

            arch = tmp_root / ARCHIVED_DEST_PATH
            arch.parent.mkdir(parents=True, exist_ok=True)
            arch.symlink_to(real_file)

            with pytest.raises(ValueError, match="symlinks forbidden"):
                resolve_imported_file_path(APPROVED_LEGACY_FILE, tmp_root)

    def test_unregistered_legacy_tests_not_exempted(self) -> None:
        """Verify all remaining 48 legacy tests are not registered and receive zero exemption."""
        legacy_48_candidates = [
            "tests/test_v4_reader.py",
            "tests/test_robinhood.py",
            "tests/test_triangular_arb.py",
            "tests/test_quoting.py",
            "tests/test_planning.py",
        ]
        for candidate in legacy_48_candidates:
            assert candidate not in LEGACY_TEST_MIGRATION_REGISTRY, (
                f"Candidate {candidate} must not be in archive migration registry"
            )
            resolved = resolve_imported_file_path(candidate, REPO_ROOT)
            assert resolved == REPO_ROOT / candidate, (
                f"Unregistered path must resolve to original disk location: {resolved}"
            )

    def test_archived_file_is_not_python_executable_module(self) -> None:
        """Verify archived files have .txt suffix and are not collected by pytest default discovery."""
        import fnmatch

        for orig_file, arch_path in [
            (APPROVED_LEGACY_FILE_V4, ARCHIVED_DEST_PATH_V4),
            (APPROVED_LEGACY_FILE_RPC, ARCHIVED_DEST_PATH_RPC),
        ]:
            # 1. Original file path must no longer exist in tests/
            original_full = REPO_ROOT / orig_file
            assert not original_full.exists(), (
                f"Original legacy test file {orig_file} must be removed from tests/ root"
            )

            # 2. Archived file must exist at designated destination with .txt suffix
            assert arch_path.endswith(".txt"), f"Archived file {arch_path} must end with .txt"
            archived_full = REPO_ROOT / arch_path
            assert archived_full.is_file(), (
                f"Archived destination file missing: {archived_full}"
            )
            assert archived_full.suffix == ".txt", (
                f"Archived file suffix must be .txt, got {archived_full.suffix}"
            )

            # 3. Verify archived file is NOT in pytest default python_files collection pattern
            pytest_default_patterns = ("test_*.py", "*_test.py")
            for pattern in pytest_default_patterns:
                assert not fnmatch.fnmatch(archived_full.name, pattern), (
                    f"Archived file {archived_full.name} matches pytest discovery pattern {pattern}"
                )

        # 4. Verify root test files do not include either original file, without hardcoding total count
        root_test_files = list((REPO_ROOT / "tests").glob("test_*.py"))
        assert (REPO_ROOT / APPROVED_LEGACY_FILE_V4) not in root_test_files, (
            f"{APPROVED_LEGACY_FILE_V4} must not be present in root test files"
        )
        assert (REPO_ROOT / APPROVED_LEGACY_FILE_RPC) not in root_test_files, (
            f"{APPROVED_LEGACY_FILE_RPC} must not be present in root test files"
        )
        assert len(root_test_files) > 0, "Root tests/ directory must contain active test files"
        assert all(p.suffix == ".py" for p in root_test_files), (
            "All root test files must remain .py modules"
        )

    def test_parent_directory_symlink_rejected_even_with_identical_bytes(
        self, tmp_path: Path
    ) -> None:
        """Verify that symlinking parent/ancestor directory to external target is rejected fail-closed,

        even when the external file preserves exact identical byte contents and SHA-256 hash.
        This proves that content hash verification alone cannot replace structural symlink denial.
        """
        # 1. Prepare external directory with audited archive bytes
        outside_dir = tmp_path / "external_store" / "robinhood"
        outside_dir.mkdir(parents=True, exist_ok=True)
        outside_file = outside_dir / "test_v4_poolkey.py.txt"
        genuine_bytes = (REPO_ROOT / ARCHIVED_DEST_PATH).read_bytes()
        outside_file.write_bytes(genuine_bytes)

        assert compute_sha256(outside_file) == AUDITED_ARCHIVE_SHA, (
            "External file must possess identical valid hash"
        )

        # 2. Stage fake repo root where parent directory 'robinhood' is a symlink pointing outside
        fake_root = tmp_path / "repo_with_parent_symlink"
        docs_legacy = fake_root / "docs" / "legacy_tests"
        docs_legacy.mkdir(parents=True, exist_ok=True)
        (docs_legacy / "robinhood").symlink_to(outside_dir)

        target_under_fake = fake_root / ARCHIVED_DEST_PATH
        assert target_under_fake.exists(), "Target file is reachable via parent symlink"
        assert target_under_fake.is_file(), "Target appears as a file via symlink dereference"
        assert compute_sha256(target_under_fake) == AUDITED_ARCHIVE_SHA

        # 3. Path resolution must fail-closed due to parent symlink
        with pytest.raises(ValueError, match="symlinks forbidden"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, fake_root)

        # 4. Audit must fail-closed and record unexpected mismatch / error
        manifest_dir = fake_root / "docs" / "reuse"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "docs" / "reuse" / "IMPORT_MANIFEST.json", manifest_dir)

        res = audit_imported_files(fake_root, allow_known_adaptations=True)
        assert res["passed"] is False
        error_records = [
            m for m in res["unexpected_mismatches"] if m["path"] == APPROVED_LEGACY_FILE
        ]
        assert len(error_records) == 1
        assert "symlinks forbidden" in error_records[0]["error"]

    def test_broken_symlink_ancestor_and_leaf_rejected_fail_closed(
        self, tmp_path: Path
    ) -> None:
        """Verify dangling/broken symlinks in ancestor path or leaf file are rejected fail-closed."""
        # 1. Broken ancestor directory symlink pointing to nonexistent path
        broken_ancestor_root = tmp_path / "broken_ancestor_repo"
        ancestor_parent = broken_ancestor_root / "docs" / "legacy_tests"
        ancestor_parent.mkdir(parents=True, exist_ok=True)
        (ancestor_parent / "robinhood").symlink_to(tmp_path / "nonexistent_directory")

        with pytest.raises(ValueError, match="symlinks forbidden"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, broken_ancestor_root)

        # 2. Broken leaf symlink pointing to nonexistent file
        broken_leaf_root = tmp_path / "broken_leaf_repo"
        leaf_parent = broken_leaf_root / "docs" / "legacy_tests" / "robinhood"
        leaf_parent.mkdir(parents=True, exist_ok=True)
        (leaf_parent / "test_v4_poolkey.py.txt").symlink_to(tmp_path / "nonexistent_file.txt")

        with pytest.raises(ValueError, match="symlinks forbidden"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, broken_leaf_root)

    def test_normal_directory_ancestor_control_allowed(
        self, tmp_path: Path
    ) -> None:
        """Verify normal genuine directory hierarchy with verified bytes passes resolution without error."""
        normal_root = tmp_path / "normal_repo"
        dest_file = normal_root / ARCHIVED_DEST_PATH
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        genuine_bytes = (REPO_ROOT / ARCHIVED_DEST_PATH).read_bytes()
        dest_file.write_bytes(genuine_bytes)

        resolved = resolve_imported_file_path(APPROVED_LEGACY_FILE, normal_root)
        assert resolved == dest_file
        assert resolved.is_file()
        assert not resolved.is_symlink()
        assert resolved.stat().st_nlink == 1
        assert compute_sha256(resolved) == AUDITED_ARCHIVE_SHA

        # Verify ancestor directory properties are genuine and non-symlink
        parts = Path(ARCHIVED_DEST_PATH).parts
        cur = normal_root
        for part in parts[:-1]:
            cur = cur / part
            st = cur.lstat()
            assert not cur.is_symlink()
            assert cur.is_dir()
            assert st.st_mode & 0o170000 == 0o040000  # S_IFDIR

    def test_non_directory_ancestor_rejected_fail_closed(
        self, tmp_path: Path
    ) -> None:
        """Verify non-directory ancestor (e.g. regular file as parent component) is rejected fail-closed."""
        bad_root = tmp_path / "file_as_ancestor_repo"
        (bad_root / "docs").mkdir(parents=True, exist_ok=True)
        # Create legacy_tests as a regular file instead of a directory
        (bad_root / "docs" / "legacy_tests").write_text("not a directory")

        with pytest.raises(ValueError, match="got non-directory"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE, bad_root)

    def test_duplicate_archived_destination_rejected_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify duplicate archived destination target paths across registry entries are rejected fail-closed."""
        bad_entry: dict[str, Any] = dict(LEGACY_TEST_MIGRATION_REGISTRY[APPROVED_LEGACY_FILE_RPC])
        bad_entry["archived_path"] = ARCHIVED_DEST_PATH_V4
        monkeypatch.setitem(LEGACY_TEST_MIGRATION_REGISTRY, APPROVED_LEGACY_FILE_RPC, bad_entry)

        with pytest.raises(ValueError, match="Collision detected.*archived_path must be unique"):
            resolve_imported_file_path(APPROVED_LEGACY_FILE_RPC, REPO_ROOT)

        with pytest.raises(ValueError, match="Collision detected.*archived_path must be unique"):
            validate_legacy_migration_registry()
