"""Automated physical sabotage verification runner executing real mutations in an isolated workspace for W3."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

# Complete required source closure for W3 physical sabotage verification.
REQUIRED_SOURCE_FILES: tuple[str, ...] = (
    # W3 Managed Files
    "apps/settled_cycle_research.py",
    "research/settled_cycles/__init__.py",
    "research/settled_cycles/attribution.py",
    "research/settled_cycles/coverage.py",
    "research/settled_cycles/decoders.py",
    "research/settled_cycles/evidence.py",
    "research/settled_cycles/flows.py",
    "research/settled_cycles/models.py",
    "research/settled_cycles/report.py",
    "research/settled_cycles/schema-v1.json",
    "tests/settled_cycles/__init__.py",
    "tests/settled_cycles/fixtures/expected.jsonl",
    "tests/settled_cycles/fixtures/historical-index.json",
    "tests/settled_cycles/fixtures/manifest.json",
    "tests/settled_cycles/fixtures/synthetic.jsonl",
    "tests/settled_cycles/test_attribution.py",
    "tests/settled_cycles/test_cli.py",
    "tests/settled_cycles/test_coverage.py",
    "tests/settled_cycles/test_decoders.py",
    "tests/settled_cycles/test_evidence.py",
    "tests/settled_cycles/test_flows.py",
    "tests/settled_cycles/test_import_boundary.py",
    "tests/settled_cycles/test_models.py",
    "tools/w3_offline_check.py",
    "tools/w3_sabotage.py",
    # Supporting Domain Contracts & Config required by test runtime
    "arbitrage_contracts/__init__.py",
    "arbitrage_contracts/arc_extensions.py",
    "arbitrage_contracts/eligibility.py",
    "arbitrage_contracts/identity.py",
    "arbitrage_contracts/legacy_adapter.py",
    "arbitrage_contracts/opportunity.py",
    "arbitrage_contracts/quote.py",
    "arbitrage_contracts/serialization.py",
    "arbitrage_contracts/state.py",
    "pyproject.toml",
)

OPTIONAL_SOURCE_FILES: tuple[str, ...] = ("research/__init__.py",)


def _read_source_file(root: Path, relative: str) -> tuple[bytes, int]:
    """Read a regular file via directory descriptors, rejecting every symlink component and hard link.

    Prevents symlink traversal and link-copy escape.
    """
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative.startswith("/")
        or relative != "/".join(parts)
        or any(part in (".", "..") for part in parts)
    ):
        raise ValueError(f"Invalid source path: {relative}")

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
                raise ValueError(f"Source must be a regular file without hard links: {relative}")
            return source.read(), metadata.st_mode
    finally:
        os.close(directory_fd)


def _setup_isolated_workspace(tmp_root: Path, repo_root: Path) -> Path:
    """Populate an isolated workspace in /tmp strictly copying only the explicit managed source closure.

    Guarantees the host source tree remains 100% read-only and unmutated.
    Uses directory-descriptor O_NOFOLLOW / nlink==1 safe reads to reject all symlinks and hardlinks.
    Fail-closed validation ensures unmerged or partial assembly trees are rejected upfront.
    """
    if not repo_root.is_dir() or repo_root.is_symlink():
        raise ValueError(f"Repo root must be an existing regular directory: {repo_root}")

    # Validate full required source closure upfront (fail-closed)
    missing_files: list[str] = [
        rel for rel in REQUIRED_SOURCE_FILES if not (repo_root / rel).is_file()
    ]
    if missing_files:
        raise ValueError(
            f"Missing {len(missing_files)} required project assembly file(s) in {repo_root}: "
            f"{missing_files[:3]}... Assembly incomplete: expected rejection on unmerged or partial source tree."
        )

    workspace = tmp_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    for rel_path in REQUIRED_SOURCE_FILES:
        content, mode = _read_source_file(repo_root, rel_path)
        dest_path = workspace / rel_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with dest_path.open("xb") as f:
            f.write(content)
        dest_path.chmod(0o755 if mode & 0o111 else 0o644)

    for rel_path in OPTIONAL_SOURCE_FILES:
        if (repo_root / rel_path).is_file():
            content, mode = _read_source_file(repo_root, rel_path)
            dest_path = workspace / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("xb") as f:
                f.write(content)
            dest_path.chmod(0o755 if mode & 0o111 else 0o644)

    return workspace


def _run_test(workspace: Path, test_target: str) -> int:
    """Execute target pytest in the isolated workspace and return exit code."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        test_target,
        "--confcutdir=tests/settled_cycles",
        "-q",
        "-o",
        "cache_dir=/tmp/pytest_sabotage_cache",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    res = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return res.returncode


def run_experiment(
    workspace: Path,
    name: str,
    target_rel_path: str,
    original_text: str,
    mutated_text: str,
    test_target: str,
) -> bool:
    """Run an isolated sabotage experiment through strictly verified 0 -> 1 -> 0 transitions."""
    target_file = workspace / target_rel_path
    if not target_file.is_file():
        print(f"❌ Target file not found in workspace: {target_file}")
        return False

    current_text = target_file.read_text(encoding="utf-8")
    if original_text not in current_text:
        print(f"❌ Target snippet not found in {target_rel_path}: {original_text!r}")
        return False

    print(f"\n--- [Sabotage Exp] {name} ---")

    # 1. Baseline check (must be green 0)
    ret_base = _run_test(workspace, test_target)
    if ret_base != 0:
        print(f"❌ Baseline check failed before mutation (code={ret_base})")
        return False
    print("  Step 1: Baseline run PASS (code=0)")

    # 2. Apply mutation in isolated workspace copy (must fail with non-zero code)
    mutated_full_text = current_text.replace(original_text, mutated_text, 1)
    target_file.write_text(mutated_full_text, encoding="utf-8")
    try:
        ret_mutated = _run_test(workspace, test_target)
        if ret_mutated == 0:
            print("❌ Tautology alert! Test still passed after mutation (code=0)!")
            return False
        print(f"  Step 2: Mutation successfully caused test failure (code={ret_mutated})")
    finally:
        # 3. Restore original in isolated workspace copy (must return green 0)
        target_file.write_text(current_text, encoding="utf-8")

    ret_restored = _run_test(workspace, test_target)
    if ret_restored != 0:
        print(f"❌ Restoration check failed (code={ret_restored})")
        return False
    print("  Step 3: Restoration run PASS (code=0)")
    print(f"✅ {name} PASSED (0 -> 1 -> 0 verified)")
    return True


def main() -> None:
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description="W3 Physical Sabotage Verification Runner")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_DEFAULT_REPO_ROOT,
        help="Path to repository root (defaults to parent of tools directory)",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()

    print("======================================================================")
    print("🔬 W3 Settled Cycle Research Physical Sabotage Verification Runner")
    print("======================================================================")

    with tempfile.TemporaryDirectory(prefix="w3_sabotage_") as tmp_dir_str:
        tmp_root = Path(tmp_dir_str)
        workspace = _setup_isolated_workspace(tmp_root, repo_root)

        results: list[bool] = []

        # Exp 1: Sabotage flash-loan principal deduction
        snip1_clean = "if action.action_kind not in _LOAN_ACTIONS:"
        snip1_mut = "if False and action.action_kind not in _LOAN_ACTIONS:"
        results.append(
            run_experiment(
                workspace,
                "Exp 1: Flash-loan loan debt closure check sabotage",
                "research/settled_cycles/attribution.py",
                snip1_clean,
                snip1_mut,
                "tests/settled_cycles/test_attribution.py",
            )
        )

        # Exp 2: Sabotage external capital inflows deduction
        snip2_clean = (
            "net = raw_delta - capital_amount - "
            "sum(component.amount_atoms for component in independent_fees)"
        )
        snip2_mut = (
            "net = raw_delta - sum(component.amount_atoms for component in independent_fees)"
        )
        results.append(
            run_experiment(
                workspace,
                "Exp 2: External capital injection deduction sabotage",
                "research/settled_cycles/attribution.py",
                snip2_clean,
                snip2_mut,
                "tests/settled_cycles/test_attribution.py",
            )
        )

        # Exp 3: Sabotage reverted subtree pruning in callTracer
        snip3_clean = 'is_reverted = parent_is_reverted or frame.get("error") is not None'
        snip3_mut = "is_reverted = False"
        results.append(
            run_experiment(
                workspace,
                "Exp 3: Reverted subtree pruning sabotage",
                "research/settled_cycles/flows.py",
                snip3_clean,
                snip3_mut,
                "tests/settled_cycles/test_flows.py",
            )
        )

        # Exp 4: Sabotage signed int24 two's complement in decoders
        snip4_clean = "return _parse_signed(hex_or_int, 24)"
        snip4_mut = "return _parse_unsigned(hex_or_int, 24)"
        results.append(
            run_experiment(
                workspace,
                "Exp 4: Decoder int24 signed arithmetic sabotage",
                "research/settled_cycles/decoders.py",
                snip4_clean,
                snip4_mut,
                "tests/settled_cycles/test_decoders.py",
            )
        )

        # Exp 5: Sabotage CLI exit code on invalid/rejected records
        snip5_clean = "if rejected or unhandled or truncated:\n        exit_code = EXIT_PARTIAL"
        snip5_mut = "if rejected or unhandled or truncated:\n        exit_code = EXIT_COMPLETE"
        results.append(
            run_experiment(
                workspace,
                "Exp 5: CLI fail-closed rejection on invalid lines sabotage",
                "apps/settled_cycle_research.py",
                snip5_clean,
                snip5_mut,
                "tests/settled_cycles/test_cli.py",
            )
        )

    print("\n======================================================================")
    if all(results):
        print(f"🎉 ALL {len(results)} PHYSICAL SABOTAGE EXPERIMENTS PASSED!")
        print("======================================================================")
        sys.exit(0)
    else:
        failed = len([r for r in results if not r])
        print(f"❌ {failed} / {len(results)} SABOTAGE EXPERIMENTS FAILED!")
        print("======================================================================")
        sys.exit(1)


if __name__ == "__main__":
    main()
