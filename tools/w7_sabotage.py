"""Automated physical sabotage verification runner executing real mutations in an isolated workspace (C01-C30)."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

# Complete required source closure for W7 physical sabotage verification.
REQUIRED_SOURCE_FILES: tuple[str, ...] = (
    # W7 Managed Files (26 files)
    "apps/rwa_observer.py",
    "rwa_research/__init__.py",
    "rwa_research/classify.py",
    "rwa_research/codec.py",
    "rwa_research/models.py",
    "rwa_research/normalize.py",
    "rwa_research/quote_inputs.py",
    "rwa_research/report.py",
    "rwa_research/validity.py",
    "tests/rwa/conftest.py",
    "tests/rwa/test_models_codec.py",
    "tests/rwa/test_normalize.py",
    "tests/rwa/test_validity.py",
    "tests/rwa/test_quote_inputs.py",
    "tests/rwa/test_classify.py",
    "tests/rwa/test_cli_e2e.py",
    "tests/rwa/test_safety.py",
    "tests/fixtures/rwa/v1/README.md",
    "tests/fixtures/rwa/v1/manifest.json",
    "tests/fixtures/rwa/v1/models.json",
    "tests/fixtures/rwa/v1/normalize.json",
    "tests/fixtures/rwa/v1/validity.json",
    "tests/fixtures/rwa/v1/quotes.json",
    "tests/fixtures/rwa/v1/e2e.json",
    "tools/w7_offline_check.py",
    "tools/w7_sabotage.py",
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


def _read_source_file(root: Path, relative: str) -> tuple[bytes, int]:
    """Read a regular file via directory descriptors, rejecting every symlink component and hard link.

    Follows the design of scripts/test_safety_stage.py to prevent symlink traversal and link-copy escape.
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

    return workspace


def _run_test(workspace: Path, test_target: str) -> int:
    """Execute target pytest in the isolated workspace and return exit code."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        test_target,
        "--confcutdir=tests/rwa",
        "-q",
        "-o",
        "cache_dir=/tmp/pytest_sabotage_cache",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)

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
            print(f"❌ Tautology alert! Test still passed after mutation (code={ret_mutated})")
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


def run_injection_experiment(
    workspace: Path,
    name: str,
    rogue_rel_path: str,
    test_target: str,
) -> bool:
    """Run rogue file injection sabotage experiment through 0 -> 1 -> 0 transitions."""
    rogue_file = workspace / rogue_rel_path
    print(f"\n--- [Sabotage Exp] {name} ---")

    # 1. Baseline check (must be green 0)
    ret_base = _run_test(workspace, test_target)
    if ret_base != 0:
        print(f"❌ Baseline check failed before injection (code={ret_base})")
        return False
    print("  Step 1: Baseline run PASS (code=0)")

    # 2. Inject rogue unregistered file in isolated workspace
    rogue_file.parent.mkdir(parents=True, exist_ok=True)
    rogue_file.write_text("# Rogue unregistered code\n", encoding="utf-8")
    try:
        ret_mutated = _run_test(workspace, test_target)
        if ret_mutated == 0:
            print("❌ Manifest reconciliation did NOT catch rogue file injection!")
            return False
        print(f"  Step 2: Rogue file successfully caused manifest mismatch (code={ret_mutated})")
    finally:
        # 3. Remove rogue file from isolated workspace
        if rogue_file.exists():
            rogue_file.unlink()

    ret_restored = _run_test(workspace, test_target)
    if ret_restored != 0:
        print(f"❌ Restoration check failed after removing rogue file (code={ret_restored})")
        return False
    print("  Step 3: Restoration run PASS (code=0)")
    print(f"✅ {name} PASSED (0 -> 1 -> 0 verified)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="🔬 W7 RWA Research Physical Sabotage Verification Runner (Isolated Workspace)"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_DEFAULT_REPO_ROOT,
        help="Path to repository root (defaults to parent directory of tools/)",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()

    print("======================================================================")
    print("🔬 W7 RWA Research Physical Sabotage Verification Runner (Isolated Workspace)")
    print("======================================================================")
    print(f"Source repository root: {repo_root}")

    try:
        with tempfile.TemporaryDirectory(prefix="w7_sabotage_") as tmp_dir_str:
            tmp_root = Path(tmp_dir_str)
            try:
                workspace = _setup_isolated_workspace(tmp_root, repo_root)
            except (ValueError, OSError) as setup_err:
                print(f"❌ [Fail-Closed] Assembly verification rejected: {setup_err}", file=sys.stderr)
                print(
                    "Notice: Execution halted because required source files are not fully assembled.",
                    file=sys.stderr,
                )
                return 1

            results: list[bool] = []

            # Exp 1: C01 Double-multiply oracle answer
            results.append(
                run_experiment(
                    workspace,
                    "Exp 1 (C01 Oracle Feed Double Multiply Mutation)",
                    "rwa_research/normalize.py",
                    "token_price_usd = Fraction(val_answer, 10**val_feed_dec)",
                    "token_price_usd = Fraction(val_answer, 10**val_feed_dec) * shares_per_token",
                    "tests/rwa/test_normalize.py::test_c01_oracle_feed_already_token_adjusted_no_double_multiply",
                )
            )

            # Exp 2: C09 Bypass Oracle Pause
            results.append(
                run_experiment(
                    workspace,
                    "Exp 2 (C09 Oracle Pause Bypass Mutation)",
                    "rwa_research/validity.py",
                    "if oracle.oracle_paused is True:",
                    "if False and oracle.oracle_paused is True:",
                    "tests/rwa/test_validity.py::test_c09_oracle_paused_treated_as_temporarily_unavailable",
                )
            )

            # Exp 3: C17 Reciprocal Bidirectional Falsification
            results.append(
                run_experiment(
                    workspace,
                    "Exp 3 (C17 Bidirectional Falsification Mutation)",
                    "rwa_research/quote_inputs.py",
                    "is_bidirectional = z41_valid and o4z_valid",
                    "is_bidirectional = True  # Falsified via reciprocal bypass",
                    "tests/rwa/test_quote_inputs.py::test_c17_bidirectional_requirement_no_reciprocal_falsification",
                )
            )

            # Exp 4: C28 Suppress Empty Input Fail-Closed
            results.append(
                run_experiment(
                    workspace,
                    "Exp 4 (C28 Empty Input Silent Pass Mutation)",
                    "apps/rwa_observer.py",
                    '_fail("Input file is empty or contains only whitespace", exit_code=2)',
                    "sys.exit(0)  # Falsely exit 0 on empty input",
                    "tests/rwa/test_cli_e2e.py::test_c28_empty_or_whitespace_input_rejected_with_exit_2",
                )
            )

            # Exp 5: C30 Unregistered Source Injection
            results.append(
                run_injection_experiment(
                    workspace,
                    "Exp 5 (C30 Unregistered Source File Injection)",
                    "rwa_research/rogue_injected_source.py",
                    "tests/rwa/test_safety.py::test_c30_manifest_reconciliation_exact_and_injection",
                )
            )

        print("======================================================================")
        if all(results):
            print("🎉 ALL 5 SABOTAGE EXPERIMENTS SUCCESSFULLY VERIFIED (0 -> 1 -> 0)!")
            print("======================================================================\n")
            return 0
        else:
            print("❌ ONE OR MORE SABOTAGE EXPERIMENTS FAILED!")
            print("======================================================================\n")
            return 1

    except Exception as exc:
        print(f"❌ [Fail-Closed] Unexpected runner error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
