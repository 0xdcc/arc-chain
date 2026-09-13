"""Tests verifying strict 1:1 coverage between disk source files and test_safety_source_manifest.json."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.test_safety_stage import (
    MANIFEST,
    PROBE_FILE,
    PUBLIC_FILES,
    SOURCE_DIRS,
    read_source,
    source_manifest,
    stage_sources,
)


class TestManifestCoverage(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent.parent
        self.manifest_path = self.root / MANIFEST
        with open(self.manifest_path, encoding="utf-8") as f:
            self.manifest_data = json.load(f)
        self.manifest_files = set(self.manifest_data["files"])

    def _collect_disk_sources(self) -> set[str]:
        disk_files: set[str] = set()
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            parts = path.relative_to(self.root).parts
            if (
                rel.startswith(".git")
                or rel.startswith("TASK-")
                or rel == ".hermes.md"
                or any(part.startswith(".") or part == "__pycache__" for part in parts)
            ):
                continue
            if rel in PUBLIC_FILES:
                disk_files.add(rel)
            elif not rel.startswith("docs") and (parts[0] in SOURCE_DIRS and rel.endswith(".py")):
                disk_files.add(rel)
        return disk_files

    def test_all_source_files_in_manifest(self) -> None:
        """Verify no source/test/public file on disk is omitted from the manifest."""
        disk_files = self._collect_disk_sources()
        missing_in_manifest = disk_files - self.manifest_files
        self.assertEqual(
            missing_in_manifest,
            set(),
            f"Files exist on disk but missing from manifest: {sorted(missing_in_manifest)}",
        )

    def test_all_manifest_files_exist_on_disk(self) -> None:
        """Verify every file declared in the manifest actually exists on disk."""
        disk_files = self._collect_disk_sources()
        ghost_in_manifest = self.manifest_files - disk_files
        self.assertEqual(
            ghost_in_manifest,
            set(),
            f"Files declared in manifest but missing on disk: {sorted(ghost_in_manifest)}",
        )

    def test_manifest_is_sorted_and_unique(self) -> None:
        """Verify the manifest files list is strictly sorted and contains no duplicates."""
        files_list = self.manifest_data["files"]
        self.assertEqual(len(files_list), len(set(files_list)), "Manifest contains duplicate files")
        self.assertEqual(
            files_list, sorted(files_list), "Manifest files are not alphabetically sorted"
        )

    def test_staging_roundtrip_and_cleanup(self) -> None:
        """Verify stage_sources successfully populates and cleans up a clean staging directory."""
        temp_dir = Path(tempfile.mkdtemp(prefix="dex-safety-stage-test-", dir="/tmp"))
        try:
            stage_sources(self.root, temp_dir)
            probe = temp_dir / PROBE_FILE
            self.assertTrue(probe.is_file(), "Probe file was not staged")
            self.assertEqual(
                probe.read_text(encoding="utf-8"),
                "ARTIFICIAL_READONLY_PROBE\n",
                "Probe file content mismatch",
            )
            matching_probes = list(temp_dir.rglob(PROBE_FILE))
            self.assertEqual(len(matching_probes), 1, "Expected exactly one probe file")
            self.assertEqual(matching_probes[0], probe)

            staged_files = {
                p.relative_to(temp_dir).as_posix()
                for p in temp_dir.rglob("*")
                if p.is_file()
            }
            self.assertIn(PROBE_FILE, staged_files, "Probe file missing from staged files")
            staged_sources = staged_files - {PROBE_FILE}
            self.assertEqual(
                staged_sources,
                self.manifest_files,
                "Staged source file set does not match manifest files exactly",
            )
            self.assertEqual(
                staged_files,
                self.manifest_files | {PROBE_FILE},
                "Staged directory contains unexpected extra files",
            )
        finally:
            if temp_dir.is_dir():
                shutil.rmtree(temp_dir)

    def test_sabotage_unapproved_file_rejected(self) -> None:
        """Sabotage test: an unapproved file injected into manifest must be rejected fail-closed."""
        # Create a temporary modified manifest in a temp copy
        temp_root = Path(tempfile.mkdtemp(prefix="dex-sabotage-", dir="/tmp"))
        try:
            # Copy minimal files
            (temp_root / "scripts").mkdir(parents=True)
            sabotaged_mf = dict(self.manifest_data)
            sabotaged_mf["files"] = list(self.manifest_data["files"]) + [
                "arbitrage/evil_unapproved.sh"
            ]
            (temp_root / MANIFEST).write_text(json.dumps(sabotaged_mf), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                source_manifest(temp_root)
            self.assertIn("Unapproved source entry", str(ctx.exception))
        finally:
            shutil.rmtree(temp_root)


if __name__ == "__main__":
    unittest.main()
