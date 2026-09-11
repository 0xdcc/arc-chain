"""Offline EvidenceBundle loading tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from research.settled_cycles.evidence import (
    EvidenceBundle,
    deduplicate_bundles,
    load_bundle,
    load_bundle_from_directory,
    validate_bundle_invariants,
)


def bundle_data(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "chain_id": 4663,
        "tx_hash": "0x" + "d" * 64,
        "block_header": {"number": 100, "hash": "0x" + "e" * 64},
        "transaction": {"hash": "0x" + "d" * 64},
        "receipt": {
            "status": 1,
            "logs": [{"logIndex": 0, "address": "0x" + "a" * 40, "removed": False}],
        },
        "trace": {"status": "available", "root_call": {}},
        "evidence_metadata": {"source_sha256": "a" * 64},
    }
    value.update(overrides)
    return value


class EvidenceBundleTests(unittest.TestCase):
    def test_fixture_manifest_hashes_exact_files(self) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures"
        manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {"expected.jsonl", "historical-index.json", "synthetic.jsonl"},
            set(manifest["files"]),
        )
        for name, expected_hash in manifest["files"].items():
            actual_hash = hashlib.sha256((fixtures / name).read_bytes()).hexdigest()
            self.assertEqual(expected_hash, actual_hash)

    def test_historical_index_is_empty_placeholder(self) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures"
        value = json.loads((fixtures / "historical-index.json").read_text(encoding="utf-8"))
        self.assertEqual([], value["real_samples"])
        self.assertEqual("1.0.0", value["version"])

    def test_load_bundle_with_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(bundle_data()), encoding="utf-8")
            loaded = load_bundle(path)
        self.assertTrue(loaded.trace_available)
        self.assertEqual([], validate_bundle_invariants(loaded))

    def test_load_bundle_without_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(bundle_data(trace=None)), encoding="utf-8")
            loaded = load_bundle(path)
        self.assertFalse(loaded.trace_available)

    def test_unavailable_trace_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(
                json.dumps(bundle_data(trace={"status": "unavailable"})),
                encoding="utf-8",
            )
            loaded = load_bundle(path)
        self.assertFalse(loaded.trace_available)

    def test_missing_source_sha256_is_rejected(self) -> None:
        data = bundle_data()
        del data["evidence_metadata"]["source_sha256"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bundle(path)

    def test_invalid_receipt_status_is_rejected(self) -> None:
        data = bundle_data(receipt={"status": 2, "logs": []})
        with self.assertRaises(ValueError):
            EvidenceBundle.from_dict(data)

    def test_duplicate_log_index_is_rejected(self) -> None:
        data = bundle_data()
        data["receipt"]["logs"].append(data["receipt"]["logs"][0])
        with self.assertRaises(ValueError):
            validate_bundle_invariants(EvidenceBundle.from_dict(data))

    def test_missing_file_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_bundle("/tmp/does-not-exist-w3-b.json")

    def test_malformed_json_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bundle(path)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text('{"chain_id":1,"chain_id":2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bundle(path)

    def test_directory_loader_reports_errors_without_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.json").write_text(json.dumps(bundle_data()), encoding="utf-8")
            (root / "invalid.json").write_text("{", encoding="utf-8")
            results = load_bundle_from_directory(root)
            self.assertEqual(2, len(results))
            self.assertTrue(any(isinstance(result[1], EvidenceBundle) for result in results))
            self.assertTrue(any(isinstance(result[1], ValueError) for result in results))

    def test_directory_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / ".." / "outside.json"
            outside.write_text(json.dumps(bundle_data()), encoding="utf-8")
            results = load_bundle_from_directory(root)
            self.assertEqual([], results)

    def test_conflicting_block_hash_is_retained(self) -> None:
        first = EvidenceBundle.from_dict(bundle_data())
        second_data = bundle_data()
        second_data["block_header"]["hash"] = "0x" + "f" * 64
        second = EvidenceBundle.from_dict(second_data)
        unique, conflicts = deduplicate_bundles([first, second])
        self.assertEqual([first], unique)
        self.assertEqual(
            [
                {
                    "chain_id": 4663,
                    "tx_hash": "0x" + "d" * 64,
                    "block_hashes": ["0x" + "e" * 64, "0x" + "f" * 64],
                }
            ],
            conflicts,
        )

    def test_removed_logs_are_counted_and_retained(self) -> None:
        data = bundle_data()
        removed_log = {"logIndex": 1, "removed": True}
        data["receipt"]["logs"].append(removed_log)
        bundle = EvidenceBundle.from_dict(data)
        self.assertEqual(["Reorged log retained at receipt position 1"], validate_bundle_invariants(bundle))
        self.assertEqual(removed_log, bundle.receipt["logs"][1])


if __name__ == "__main__":
    unittest.main()
