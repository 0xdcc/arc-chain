"""C21 explicit file CLI, forbidden flags and read-only import closure."""

from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path

import pytest

from apps.market_catalog import main
from arbitrage_contracts import decode_record_json
from market_catalog.export import CatalogHeader, read_snapshot

ROOT = Path(__file__).resolve().parents[2]


def arguments(tmp_path: Path) -> list[str]:
    """Provide all explicit inputs without environment or default paths."""
    source = ROOT / "tests/fixtures/catalog/v1/synthetic_valid.jsonl"
    metadata = tmp_path / "metadata.json"
    header = CatalogHeader(
        "cli-test",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "v1",
        None,
        1200,
        "synthetic",
        ("synthetic:discovery",),
    ).to_dict()
    del header["catalog_schema"], header["version"]
    metadata.write_text(json.dumps(header))
    return [
        "--input",
        str(source),
        "--metadata",
        str(metadata),
        "--output",
        str(tmp_path / "output"),
    ]


def test_cli_files_to_jsonl(tmp_path: Path) -> None:
    output = io.StringIO()
    assert main(arguments(tmp_path), stdout=output) == 0
    records = [decode_record_json(line) for line in output.getvalue().splitlines()]
    assert len(records) == 9
    assert {r.record_type for r in records} == {
        "token_key",
        "asset_eligibility",
        "pool_descriptor",
        "pool_capability",
    }
    snapshot = read_snapshot(tmp_path / "output")
    assert len(snapshot.records["eligibility"]) == 4
    assert all(r.data_mode == "synthetic" for r in records)


@pytest.mark.parametrize(
    "flag", ["--help", "--broadcast", "--wallet", "--approve", "--notify", "--in"]
)
def test_cli_flags_absent_with_otherwise_valid_inputs(
    tmp_path: Path, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    output = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        main(arguments(tmp_path) + [flag], stdout=output)
    assert exc.value.code == 2
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()
    assert output.getvalue() == ""


def test_cli_malformed_input_no_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = arguments(tmp_path)
    source = tmp_path / "bad.jsonl"
    source.write_text("not-json")
    argv[1] = str(source)
    with pytest.raises(SystemExit) as exc:
        main(argv, stdout=io.StringIO())
    assert exc.value.code == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_cli_empty_input_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = arguments(tmp_path)
    source = tmp_path / "empty.jsonl"
    source.write_bytes(b"")
    argv[1] = str(source)
    metadata = Path(argv[3])
    document = json.loads(metadata.read_text())
    document["fixture_hash"] = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata.write_text(json.dumps(document))
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    output = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        main(argv, stdout=output)
    assert exc.value.code == 2
    assert "Discovery input contains no records" in capsys.readouterr().err
    assert output.getvalue() == ""
    assert not (tmp_path / "output").exists()
    assert {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before


def test_cli_review_requires_trust(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(arguments(tmp_path) + ["--review", str(tmp_path / "review")])
    assert exc.value.code == 2
    assert "supplied together" in capsys.readouterr().err


def test_cli_imports_only_readonly_closure() -> None:
    tree = ast.parse((ROOT / "apps/market_catalog.py").read_text())
    allowed = {"__future__", "argparse", "json", "sys", "pathlib", "typing", "market_catalog"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.module.split(".")[0] in allowed
