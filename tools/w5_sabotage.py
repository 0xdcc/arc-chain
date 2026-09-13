"""W5 Sabotage Mutation Verification Tool (TASK-W5-G-v1, C24).

Executes automated physical mutation sabotage experiments in an isolated /tmp workspace:
1. C07 (Rounding / Ceiling & Single Deduction): Mutate rounding/floor in policy calculator -> FAIL -> Restore -> PASS.
2. C10 (Dual Decoded Verification): Bypass minOut equality check in calldata verifier -> FAIL -> Restore -> PASS.
3. C14 (Fixed Block Pinned Tag Defense): Bypass unpinned latest/pending block tag check -> FAIL -> Restore -> PASS.
4. C16 (Quoter Masquerade Defense): Bypass Universal Router target quoter check -> FAIL -> Restore -> PASS.
5. C21 (CLI Live/Destructive Dispatch Interception): Bypass CLI live/send guard -> FAIL -> Restore -> PASS.
6. C22 (Directory Traversal & Symlink Interception): Bypass path component symlink check -> FAIL -> Restore -> PASS.

Strict Invariant Asserted:
Each mutation MUST transition: PASS (0) -> MUTATION FAIL (!=0) -> RESTORE PASS (0).
Operates exclusively on a temporary managed source copy in /tmp; host source tree remains 100% read-only.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = sys.executable

# Complete required source closure for W5 physical sabotage verification.
REQUIRED_SOURCE_FILES: tuple[str, ...] = (
    # W5 Managed Files (30 files)
    "apps/atomic_simulate.py",
    "atomic_execution/__init__.py",
    "atomic_execution/encoding.py",
    "atomic_execution/export.py",
    "atomic_execution/inputs.py",
    "atomic_execution/models.py",
    "atomic_execution/pipeline.py",
    "atomic_execution/planning.py",
    "atomic_execution/policy.py",
    "atomic_execution/reconciliation.py",
    "atomic_execution/simulation.py",
    "atomic_execution/transport.py",
    "tests/atomic_execution/test_boundary.py",
    "tests/atomic_execution/test_cli.py",
    "tests/atomic_execution/test_encoding.py",
    "tests/atomic_execution/test_inputs.py",
    "tests/atomic_execution/test_pipeline.py",
    "tests/atomic_execution/test_policy.py",
    "tests/atomic_execution/test_reconciliation.py",
    "tests/atomic_execution/test_simulation.py",
    "tests/fixtures/atomic_execution/v1/README.md",
    "tests/fixtures/atomic_execution/v1/e2e-stream.jsonl",
    "tests/fixtures/atomic_execution/v1/encoding-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/input-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/manifest.json",
    "tests/fixtures/atomic_execution/v1/policy-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/receipt-cases.jsonl",
    "tests/fixtures/atomic_execution/v1/simulation-cases.jsonl",
    "tools/w5_offline_check.py",
    "tools/w5_sabotage.py",
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

OPTIONAL_SOURCE_FILES: tuple[str, ...] = (
    "tests/atomic_execution/test_audit_remediation_w5.py",
    "atomic_execution/arc_encoding.py",
    "atomic_execution/arc_output_adapter.py",
    "atomic_execution/arc_planning.py",
    "atomic_execution/arc_simulation.py",
    "atomic_execution/arc_transport.py",
    "atomic_execution/deployments.py",
    "atomic_execution/output_evidence.py",
    # Real dependency closure for deployments.py and output_evidence.py
    "arc_readiness/errors.py",
    "arc_opportunities/__init__.py",
    "arc_opportunities/cost_units.py",
)


@dataclass(frozen=True, slots=True)
class SabotageExperiment:
    """Definition of an automated physical mutation experiment."""

    id: str
    name: str
    criterion: str
    target_file: str
    clean_snippet: str
    mutated_snippet: str
    pytest_filter: str


EXPERIMENTS: tuple[SabotageExperiment, ...] = (
    # 1. C07 Rounding Ceiling / Single Deduction
    SabotageExperiment(
        id="EXP_C07_ROUNDING",
        name="C07 Rounding Ceiling & 1 Atom Floor",
        criterion="C07",
        target_file="atomic_execution/policy.py",
        clean_snippet="# Upward rounding (ROUND_CEILING) of gas cost in base atoms\n    gas_atoms = int(((gas_usd * scale) / price).to_integral_value(rounding=ROUND_CEILING))",
        mutated_snippet="# Upward rounding (ROUND_CEILING) of gas cost in base atoms\n    gas_atoms = int(((gas_usd * scale) / price).to_integral_value(rounding='ROUND_FLOOR'))",
        pytest_filter="test_c07_round_ceiling_precision_conversion",
    ),
    # 2. C10 Dual Decoded Verification
    SabotageExperiment(
        id="EXP_C10_DUAL_DECODE",
        name="C10 Dual Decoded Calldata Verification",
        criterion="C10",
        target_file="atomic_execution/encoding.py",
        clean_snippet='if decoded_input.get("deadline") != target_deadline:',
        mutated_snippet='if False and decoded_input.get("deadline") != target_deadline:',
        pytest_filter="test_c10_dual_decoded_deadline_mismatch_fails",
    ),
    # 3. C14 Fixed Block Height & Tag Defense
    SabotageExperiment(
        id="EXP_C14_FIXED_BLOCK",
        name="C14 Unpinned Block Tag Rejection",
        criterion="C14",
        target_file="atomic_execution/simulation.py",
        clean_snippet='if block_hash.strip().lower() in ("latest", "pending", "earliest"):',
        mutated_snippet='if False and block_hash.strip().lower() in ("latest", "pending", "earliest"):',
        pytest_filter="test_c14_unpinned_latest_tag_rejected",
    ),
    # 4. C16 Quoter Masquerade Defense
    SabotageExperiment(
        id="EXP_C16_QUOTER_MASQUERADE",
        name="C16 Quoter Address Masquerade Defense",
        criterion="C16",
        target_file="atomic_execution/simulation.py",
        clean_snippet="if normalized_router.lower() in KNOWN_QUOTER_ADDRESSES:",
        mutated_snippet="if False and normalized_router.lower() in KNOWN_QUOTER_ADDRESSES:",
        pytest_filter="test_c16_quoter_v3_target_address_rejected",
    ),
    # 5. C21 CLI Live Flag Fail-Closed Interception
    SabotageExperiment(
        id="EXP_C21_CLI_LIVE_GUARD",
        name="C21 CLI Live/Destructive Dispatch Interception",
        criterion="C21",
        target_file="apps/atomic_simulate.py",
        clean_snippet="if flag_candidate in FORBIDDEN_DISPATCH_FLAGS:",
        mutated_snippet="if False and flag_candidate in FORBIDDEN_DISPATCH_FLAGS:",
        pytest_filter="test_c21_forbidden_flags_intercepted_in_process",
    ),
    # 6. C22 Symlink & Directory Traversal Interception
    SabotageExperiment(
        id="EXP_C22_SYMLINK_GUARD",
        name="C22 Symlink Component Rejection",
        criterion="C22",
        target_file="atomic_execution/export.py",
        clean_snippet="if component.is_symlink() or os.path.islink(component):",
        mutated_snippet="if False and (component.is_symlink() or os.path.islink(component)):",
        pytest_filter="test_c22_export_parent_directory_symlink_rejected",
    ),
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

    for rel_path in OPTIONAL_SOURCE_FILES:
        if (repo_root / rel_path).is_file():
            content, mode = _read_source_file(repo_root, rel_path)
            dest_path = workspace / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("xb") as f:
                f.write(content)
            dest_path.chmod(0o755 if mode & 0o111 else 0o644)

    return workspace


def _run_isolated_test(workspace: Path, pytest_filter: str) -> int:
    """Execute target pytest in the isolated workspace and return exit code."""
    cmd = [
        PYTHON_BIN,
        "-m",
        "pytest",
        "tests/atomic_execution/",
        "-k",
        pytest_filter,
        "--confcutdir=tests/atomic_execution",
        "-o",
        "cache_dir=/tmp/pytest_sabotage_cache",
        "-q",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.returncode


def run_sabotage_experiment(workspace: Path, exp: SabotageExperiment) -> bool:
    """Execute single physical sabotage experiment through 0 -> !=0 -> 0 transitions."""
    target_path = workspace / exp.target_file
    if not target_path.is_file():
        print(f"  [ERROR] Target file not found: {target_path}")
        return False

    original_code = target_path.read_text(encoding="utf-8")
    if exp.clean_snippet not in original_code:
        print(f"  [ERROR] Clean snippet not found in {exp.target_file}: {exp.clean_snippet!r}")
        return False

    print(f"Running {exp.id} ({exp.name}):")

    # Step 1: Baseline PASS (0)
    code_baseline = _run_isolated_test(workspace, exp.pytest_filter)
    if code_baseline != 0:
        print(
            f"  [FAIL] Step 1 Baseline PASS assertion failed (got exit {code_baseline}, expected 0)"
        )
        return False
    print("  -> Step 1 Baseline: PASS (exit 0)")

    # Step 2: Physical Mutation -> FAIL (!=0)
    mutated_code = original_code.replace(exp.clean_snippet, exp.mutated_snippet, 1)
    target_path.write_text(mutated_code, encoding="utf-8")
    try:
        code_mutated = _run_isolated_test(workspace, exp.pytest_filter)
        if code_mutated == 0:
            print(
                "  [FAIL] Step 2 Mutation Sabotage failed to catch bug (got exit 0, expected non-zero)"
            )
            return False
        print(f"  -> Step 2 Sabotage: FAIL (exit {code_mutated}) [Mutation Caught!]")
    finally:
        # Step 3: Revert Mutation -> Recovery PASS (0)
        target_path.write_text(original_code, encoding="utf-8")

    code_recovered = _run_isolated_test(workspace, exp.pytest_filter)
    if code_recovered != 0:
        print(
            f"  [FAIL] Step 3 Recovery PASS assertion failed (got exit {code_recovered}, expected 0)"
        )
        return False
    print("  -> Step 3 Recovery: PASS (exit 0)")

    print(f"  => {exp.id} verified strictly: 0 -> !=0 -> 0\n")
    return True


def main() -> int:
    """Execute all 6 sabotage physical mutation experiments in an isolated workspace."""
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description="W5 Physical Sabotage Verification Runner")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_DEFAULT_REPO_ROOT,
        help="Path to repository root (defaults to parent of tools directory)",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()

    print("================================================================================")
    print("W5 PHYSICAL SABOTAGE MUTATION SUITE (TASK-W5-G-v1, C24)")
    print("================================================================================")
    print(f"Total experiments configured: {len(EXPERIMENTS)}\n")

    start_time = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="w5_sabotage_") as tmp_dir_str:
        tmp_root = Path(tmp_dir_str)
        workspace = _setup_isolated_workspace(tmp_root, repo_root)

        all_passed = True
        for exp in EXPERIMENTS:
            success = run_sabotage_experiment(workspace, exp)
            if not success:
                all_passed = False
                break

    elapsed = time.perf_counter() - start_time
    print("================================================================================")
    if all_passed:
        print(
            f"RESULT: ALL {len(EXPERIMENTS)} SABOTAGE EXPERIMENTS PROVED (0 -> !=0 -> 0) in {elapsed:.2f}s (EXIT 0)"
        )
        print("================================================================================")
        return 0

    print(f"RESULT: SABOTAGE MUTATION FAILED in {elapsed:.2f}s (EXIT 1)")
    print("================================================================================")
    return 1


if __name__ == "__main__":
    sys.exit(main())
