#!/usr/bin/env python3
"""Offline local audit runner for Arc-Chain.

Provides an offline, credential-isolated diagnostic harness replacing remote CI workflows.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_SUBSTRINGS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "AUTH",
    "CREDENTIAL",
    "TELEGRAM",
    "DISCORD",
    "HERMES",
    "WEBHOOK",
    "SENTINEL",
)


@dataclasses.dataclass(frozen=True)
class CheckResult:
    label: str
    cmd: list[str]
    exit_code: int
    timed_out: bool
    duration_seconds: float
    stdout: str
    stderr: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cmd": self.cmd,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


@dataclasses.dataclass(frozen=True)
class AuditReport:
    repo_root: str
    interpreter: str
    evidence_dir: str
    passed: bool
    total_checks: int
    passed_checks: int
    failed_checks: int
    results: list[CheckResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "interpreter": self.interpreter,
            "evidence_dir": self.evidence_dir,
            "passed": self.passed,
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "results": [result_item.to_dict() for result_item in self.results],
        }


def build_clean_env(
    base_env: dict[str, str] | None = None,
    interpreter: Path | None = None,
) -> dict[str, str]:
    """Construct an isolated environment without sensitive credentials or bytecode caching."""
    source_env = os.environ if base_env is None else base_env
    clean_env: dict[str, str] = {}
    for env_key, env_val in source_env.items():
        key_upper = env_key.upper()
        if any(needle in key_upper for needle in _SENSITIVE_KEY_SUBSTRINGS):
            continue
        clean_env[env_key] = env_val

    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    clean_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    if interpreter is not None:
        bin_dir = interpreter.parent
        existing_path = clean_env.get("PATH", "/usr/bin:/bin")
        if str(bin_dir) not in existing_path.split(os.pathsep):
            clean_env["PATH"] = f"{bin_dir}{os.pathsep}{existing_path}"
        venv_dir = bin_dir.parent
        if (venv_dir / "pyvenv.cfg").is_file():
            clean_env["VIRTUAL_ENV"] = str(venv_dir)

    return clean_env


def build_audit_checks(
    repo_root: Path, interpreter: Path, evidence_dir: Path
) -> list[tuple[str, list[str]]]:
    """Build check definitions matching the original CI diagnostic workflow."""
    resolved_evidence = evidence_dir.resolve()
    interp_str = str(interpreter)

    pytest_arc_cache = resolved_evidence / "pytest_cache_arc"
    pytest_col_cache = resolved_evidence / "pytest_cache_collection"
    mypy_cache_dir = resolved_evidence / "mypy_cache"

    return [
        (
            "arc-tests",
            [
                interp_str,
                "-m",
                "pytest",
                "tests/arc_v3/",
                "-q",
                "--tb=short",
                "-o",
                f"cache_dir={pytest_arc_cache}",
            ],
        ),
        (
            "contracts",
            [
                interp_str,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests/contracts",
                "-v",
            ],
        ),
        (
            "full-collection",
            [
                interp_str,
                "-m",
                "pytest",
                "tests/",
                "--collect-only",
                "-q",
                "--tb=short",
                "-o",
                f"cache_dir={pytest_col_cache}",
            ],
        ),
        (
            "lint",
            [
                interp_str,
                "-m",
                "ruff",
                "check",
                "--no-cache",
                ".",
            ],
        ),
        (
            "typing",
            [
                interp_str,
                "-m",
                "mypy",
                f"--cache-dir={mypy_cache_dir}",
                ".",
            ],
        ),
    ]


def execute_single_check(
    label: str,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    evidence_dir: Path,
    timeout_seconds: int = 240,
) -> CheckResult:
    """Run one diagnostic command and persist complete multi-line logs and exit code."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_file = evidence_dir / f"{label}.log"

    start_time = time.monotonic()
    timed_out = False
    error_msg: str | None = None
    stdout = ""
    stderr = ""
    exit_code = 0

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
            error_msg = f"Command timed out after {timeout_seconds} seconds"
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=2.0)
                except subprocess.TimeoutExpired as kill_exc:
                    if proc.stdout:
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass
                    if proc.stderr:
                        try:
                            proc.stderr.close()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=0.5)
                    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                        pass
                    raw_out: Any = kill_exc.output
                    raw_err: Any = kill_exc.stderr
                    if isinstance(raw_out, bytes):
                        stdout = raw_out.decode("utf-8", errors="replace")
                    elif isinstance(raw_out, str):
                        stdout = raw_out
                    else:
                        stdout = ""
                    if isinstance(raw_err, bytes):
                        stderr = raw_err.decode("utf-8", errors="replace")
                    elif isinstance(raw_err, str):
                        stderr = raw_err
                    else:
                        stderr = ""
                    error_msg = (
                        f"Command timed out after {timeout_seconds} seconds "
                        "(cleanup communicate timed out; pipes force-closed, possible orphaned processes or held descriptors)"
                    )
    except FileNotFoundError as exc:
        exit_code = 127
        error_msg = f"Executable or file not found: {exc}"
        stderr = str(exc)
    except Exception as exc:
        exit_code = 1
        error_msg = f"Execution failed with unexpected error: {exc}"
        stderr = str(exc)

    stdout = stdout or ""
    stderr = stderr or ""

    duration = time.monotonic() - start_time

    log_sections = [
        f"=== CMD: {" ".join(cmd)} ===",
        f"=== EXIT: {exit_code} ===",
        "=== STDOUT ===",
        stdout,
        "=== STDERR ===",
        stderr,
    ]
    if error_msg:
        log_sections.extend(["=== ERROR ===", error_msg])
    log_sections.append(f"AUDIT_RESULT {label} exit={exit_code}")

    full_log_content = "\n".join(log_sections) + "\n"
    log_file.write_text(full_log_content, encoding="utf-8")

    print(full_log_content)

    return CheckResult(
        label=label,
        cmd=cmd,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=duration,
        stdout=stdout,
        stderr=stderr,
        error=error_msg,
    )


def run_audit(
    repo_root: Path | str,
    interpreter: Path | str,
    evidence_dir: Path | str,
    timeout_seconds: int = 240,
    labels: Sequence[str] | None = None,
    base_env: dict[str, str] | None = None,
) -> AuditReport:
    """Run configured offline audit checks and produce aggregated results."""
    root_path = Path(repo_root).resolve()
    interp_path = Path(interpreter)
    evid_path = Path(evidence_dir).resolve()

    evid_path.mkdir(parents=True, exist_ok=True)
    clean_env = build_clean_env(base_env, interpreter=interp_path)

    all_checks = build_audit_checks(root_path, interp_path, evid_path)
    if labels:
        wanted = set(labels)
        all_checks = [check_item for check_item in all_checks if check_item[0] in wanted]

    results: list[CheckResult] = []
    for check_label, check_cmd in all_checks:
        result = execute_single_check(
            label=check_label,
            cmd=check_cmd,
            cwd=root_path,
            env=clean_env,
            evidence_dir=evid_path,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)

    passed_checks = sum(1 for item in results if item.exit_code == 0 and not item.timed_out)
    total_checks = len(results)
    failed_checks = total_checks - passed_checks
    overall_passed = failed_checks == 0 and total_checks > 0

    report = AuditReport(
        repo_root=str(root_path),
        interpreter=str(interp_path),
        evidence_dir=str(evid_path),
        passed=overall_passed,
        total_checks=total_checks,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        results=results,
    )

    summary_path = evid_path / "audit_summary.json"
    summary_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    print("No live RPC, signing, broadcast, or deployment was requested by this audit runner.\n")
    print(f"AUDIT SUMMARY: {passed_checks}/{total_checks} checks passed (passed={overall_passed})")

    return report


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Arc-Chain offline local audit diagnostics runner"
    )
    default_root = Path(__file__).resolve().parents[2]
    default_interp = default_root / "venv" / "bin" / "python"
    if not default_interp.exists():
        parent_interp = default_root.parents[1] / "venv" / "bin" / "python"
        if parent_interp.exists():
            default_interp = parent_interp
        else:
            default_interp = Path(sys.executable)

    parser.add_argument(
        "--repo-root",
        type=Path,
        default=default_root,
        help="Target repository root directory (default: project root)",
    )
    parser.add_argument(
        "--interpreter",
        type=Path,
        default=default_interp,
        help="Python interpreter path (default: ./venv/bin/python or sys.executable)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("/tmp/arc-audit-r3-evidence"),
        help="Output directory for logs and JSON summary (default: /tmp/arc-audit-r3-evidence)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="Timeout in seconds per check (default: 240)",
    )
    parser.add_argument(
        "--labels",
        nargs="*",
        default=None,
        help="Optional subset of check labels to run (e.g. arc-tests lint)",
    )
    return parser.parse_args(args)


def main(args: Sequence[str] | None = None) -> int:
    parsed = parse_args(args)
    report = run_audit(
        repo_root=parsed.repo_root,
        interpreter=parsed.interpreter,
        evidence_dir=parsed.evidence_dir,
        timeout_seconds=parsed.timeout,
        labels=parsed.labels,
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
