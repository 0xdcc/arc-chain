"""Stage only reviewed regular source files without following source symlinks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath

MANIFEST = "scripts/test_safety_source_manifest.json"
PROBE_FILE = "sandbox_probe_fixture.txt"
PUBLIC_FILES = {
    "AGENTS.md",
    "check.sh",
    "pyproject.toml",
    "requirements.txt",
    "run_arbitrage.py",
    "run_monitor.py",
    "run_live_pipeline.py",
    "sandbox_probe_fixture.txt",
    "core/rpc_manifest.json",
    "arbitrage/v4_pools_manifest.json",
    "schemas/arbitrage-evidence-v1.json",
    "scripts/test_safety_mypy_baseline.txt",
    "scripts/test_safety_sandbox.sh",
    "tests/fixtures/arc_readiness/balance_vectors.json",
    "tests/fixtures/arc_readiness/bridge_vectors.json",
    "tests/fixtures/arc_readiness/catalog_vectors.json",
    "tests/fixtures/arc_readiness/e2e_stream.json",
    "tests/fixtures/arc_readiness/event_vectors.json",
    "tests/fixtures/arc_readiness/manifest.json",
    "tests/fixtures/arc_readiness/rpc_vectors.json",
    "tests/fixtures/contracts/v1/legacy.jsonl",
    "tests/fixtures/catalog/v1/README.md",
    "tests/fixtures/catalog/v1/manifest.json",
    "tests/fixtures/catalog/v1/synthetic_changes.jsonl",
    "tests/fixtures/catalog/v1/synthetic_invalid.jsonl",
    "tests/fixtures/catalog/v1/synthetic_quotes.jsonl",
    "tests/fixtures/catalog/v1/synthetic_valid.jsonl",
    "tests/fixtures/opportunities/v1/all-negative-ledger.jsonl",
    "tests/fixtures/opportunities/v1/economic-cases.jsonl",
    "tests/fixtures/opportunities/v1/historical-rpc.jsonl",
    "tests/fixtures/opportunities/v1/synthetic-stream.jsonl",
    "tests/fixtures/opportunities/v1/w2e-final-ledger.jsonl",
    "tests/fixtures/opportunities/v1/shadow-candidates.json",
    "tests/fixtures/opportunities/v1/shadow-manifest.json",
    "tests/fixtures/opportunities/v1/shadow-registry.json",
    "tests/fixtures/rwa/v1/README.md",
    "tests/fixtures/rwa/v1/e2e.json",
    "tests/fixtures/rwa/v1/manifest.json",
    "tests/fixtures/rwa/v1/models.json",
    "tests/fixtures/rwa/v1/normalize.json",
    "tests/fixtures/rwa/v1/quotes.json",
    "tests/fixtures/rwa/v1/validity.json",
    "tests/fixtures/state_graph/v2/README.md",
    "tests/fixtures/state_graph/v2/base.json",
    "tests/fixtures/state_graph/v2/e2e.json",
    "tests/fixtures/state_graph/v2/evaluate.json",
    "tests/fixtures/state_graph/v2/graph.json",
    "tests/fixtures/state_graph/v2/index.json",
    "tests/fixtures/state_graph/v2/manifest.json",
    "tests/fixtures/state_graph/v2/reference.json",
    "tests/opportunities/test_candidates.py",
    "tests/opportunities/test_cli_e2e.py",
    "tests/opportunities/test_import_boundary.py",
    "tests/opportunities/test_input_gate.py",
    "tests/opportunities/test_replay_child.py",
    "tests/opportunities/test_report.py",
    "tests/opportunities/test_report_cli.py",
    "tests/opportunities/test_shadow_child.py",
    "tests/opportunities/test_shadow.py",
    "tests/opportunities/test_single_hop.py",
    "tests/opportunities/test_quote_adapter.py",
    "tests/opportunities/test_w2g_sabotage.py",
    "tests/fixtures/atomic_execution/v1/README.md",
    "tests/fixtures/atomic_execution/v1/e2e-stream.jsonl",
    "tests/fixtures/atomic_execution/v1/encoding-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/input-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/manifest.json",
    "tests/fixtures/atomic_execution/v1/policy-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/receipt-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/simulation-cases.jsonl",
    "research/settled_cycles/schema-v1.json",
    "tests/settled_cycles/fixtures/manifest.json",
    "tests/settled_cycles/fixtures/synthetic.jsonl",
    "tests/settled_cycles/fixtures/expected.jsonl",
    "tests/settled_cycles/fixtures/historical-index.json",
    MANIFEST,
}
SOURCE_DIRS = {
    "apps",
    "arbitrage",
    "arbitrage_contracts",
    "arc_readiness",
    "atomic_execution",
    "backtest",
    "chains",
    "core",
    "execution",
    "market_catalog",
    "monitors",
    "opportunities",
    "research",
    "rwa_research",
    "scripts",
    "state_graph",
    "tests",
    "tools",
}


def read_source(root: Path, relative: str) -> tuple[bytes, int]:
    """Read a regular file via directory descriptors, rejecting every symlink component."""
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative.startswith("/")
        or relative != "/".join(parts)
        or any(part in (".", "..") for part in parts)
    ):
        raise ValueError("Invalid source path")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(file_fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Source must be a regular file without hard links")
            return source.read(), metadata.st_mode
    finally:
        os.close(directory_fd)


def source_manifest(root: Path) -> list[str]:
    """Validate the explicit manifest before opening any listed source content."""
    manifest = json.loads(read_source(root, MANIFEST)[0])
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) != len(set(files)):
        raise ValueError("Invalid or duplicate manifest entries")
    for relative in files:
        parts = PurePosixPath(relative).parts
        if (
            not parts
            or relative.startswith("/")
            or relative != "/".join(parts)
            or any(part.startswith(".") or part == "__pycache__" for part in parts)
            or not (
                relative in PUBLIC_FILES or (parts[0] in SOURCE_DIRS and relative.endswith(".py"))
            )
        ):
            raise ValueError(f"Unapproved source entry: {relative}")
    return files


def stage_sources(root: Path, destination: Path) -> None:
    """Copy the explicit file list into an empty private staging directory."""
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Staging destination must be an empty regular directory")
    for relative in source_manifest(root):
        content, mode = read_source(root, relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)
        target.chmod(0o755 if mode & 0o111 else 0o644)
    (destination / PROBE_FILE).write_text("ARTIFICIAL_READONLY_PROBE\n", encoding="utf-8")


def main() -> int:
    """Stage sources or remove the launcher's own private temporary directory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    try:
        if args.cleanup:
            target = args.destination
            if target.parent != Path("/tmp") or not target.name.startswith("dex-safety-stage-"):
                raise ValueError("Invalid cleanup target")
            if target.is_symlink():
                raise ValueError("Symlink cleanup target rejected")
            shutil.rmtree(target)
        else:
            stage_sources(args.source, args.destination)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"[Fail-Closed] Source staging rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
