"""W3-E offline CLI and report regression tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from apps.settled_cycle_research import (
    EXIT_COMPLETE,
    EXIT_CONFLICT,
    EXIT_PARTIAL,
    EXIT_USAGE,
    main,
)
from research.settled_cycles.report import write_outputs


def registry_value() -> dict[str, Any]:
    return {"schema": "w3-pool-registry/1.0.0", "pools": []}


def write_registry(path: Path) -> None:
    path.write_text(json.dumps(registry_value()), encoding="utf-8")


class ReportAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.input = self.root / "tests/settled_cycles/fixtures/synthetic.jsonl"

    def test_write_outputs_rejects_non_empty_directory(self) -> None:
        from research.settled_cycles.models import SettledCycleRecord

        record = SettledCycleRecord.from_dict(json.loads(self.input.read_text().splitlines()[0]))
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "existing.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not empty"):
                write_outputs([record], out_dir, registry=registry_value())

    def test_write_outputs_is_relocation_stable(self) -> None:
        from research.settled_cycles.models import SettledCycleRecord

        record = SettledCycleRecord.from_dict(json.loads(self.input.read_text().splitlines()[0]))
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one"
            second = Path(directory) / "two"
            write_outputs([record], first, registry=registry_value())
            write_outputs([record], second, registry=registry_value())
            self.assertEqual(
                hashlib.sha256((first / "records.jsonl").read_bytes()).hexdigest(),
                hashlib.sha256((second / "records.jsonl").read_bytes()).hexdigest(),
            )

    def test_cli_complete_truncated_and_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_registry(root / "registry.json")
            out = root / "out"
            code = main(
                [
                    "--input",
                    str(self.input),
                    "--registry",
                    str(root / "registry.json"),
                    "--output-dir",
                    str(out),
                    "--mode",
                    "offline",
                ]
            )
            self.assertEqual(EXIT_COMPLETE, code)
            self.assertEqual(6, len((out / "records.jsonl").read_text().splitlines()))
            manifest = json.loads((out / "run-manifest.json").read_text())
            self.assertFalse(manifest["truncated"])

    def test_cli_conflict_returns_four(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_registry(root / "registry.json")
            out = root / "out"
            out.mkdir()
            (out / "blocker").write_text("existing", encoding="utf-8")
            code = main(
                [
                    "--input",
                    str(self.input),
                    "--registry",
                    str(root / "registry.json"),
                    "--output-dir",
                    str(out),
                    "--mode",
                    "offline",
                ]
            )
            self.assertEqual(EXIT_CONFLICT, code)

    def test_cli_bad_format_and_non_offline_return_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_registry(root / "registry.json")
            bad = root / "bad.jsonl"
            bad.write_text("{not-json}\n", encoding="utf-8")
            common = ["--registry", str(root / "registry.json"), "--output-dir", str(root / "out")]
            self.assertEqual(EXIT_PARTIAL, main(["--input", str(bad), *common, "--mode", "offline"]))
            self.assertEqual(EXIT_USAGE, main(["--input", str(self.input), *common, "--mode", "online"]))

    def test_cli_truncation_and_invalid_evidence_are_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_registry(root / "registry.json")
            partial = root / "partial.jsonl"
            lines = self.input.read_text(encoding="utf-8").splitlines()
            lines.append(json.dumps({"truncated": True, "reason": "source_stream_ended"}))
            lines.append(json.dumps({"schema_id": "invalid"}))
            partial.write_text("\n".join(lines) + "\n", encoding="utf-8")
            out = root / "out"
            code = main(
                [
                    "--input",
                    str(partial),
                    "--registry",
                    str(root / "registry.json"),
                    "--output-dir",
                    str(out),
                    "--mode",
                    "offline",
                ]
            )
            manifest = json.loads((out / "run-manifest.json").read_text())
            self.assertEqual(EXIT_PARTIAL, code)
            self.assertTrue(manifest["truncated"])
            self.assertEqual("source_stream_ended", manifest["termination_reason"])
            self.assertEqual(1, manifest["counts"]["rejected"])


if __name__ == "__main__":
    unittest.main()
