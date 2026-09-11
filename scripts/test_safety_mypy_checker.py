#!/usr/bin/env python3
"""Strict Mypy baseline validator and fail-closed gate for check.sh.

Evaluates mypy execution against known baseline errors file with strict fail-closed semantics:
1. Rejects any non-0/1 exit codes (internal crashes, fatal signals, environment faults).
2. Rejects exit code 1 if any unparseable output, Traceback, INTERNAL ERROR, or usage errors appear.
3. Rejects exit code 0 if any error diagnostics are emitted.
4. Rejects exit code 1 if zero error diagnostics are emitted.
5. Rejects any diagnostic error not present in the baseline.
6. Strictly accepts only when all output lines are valid diagnostics (matching baseline), notes,
   or recognized summaries, with zero tool anomalies.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Regex patterns for valid mypy diagnostic and summary lines
_RE_ERROR = re.compile(r"^.+:\d+(?::\d+)?: error: .+$")
_RE_NOTE = re.compile(r"^.+:\d+(?::\d+)?: note: .+$")
_RE_SUMMARY = re.compile(r"^(?:Found \d+ errors? in \d+ files?.*|Success: no issues found.*)$")

# Case-insensitive markers indicating tool crashes, unhandled exceptions, or CLI syntax errors
_CRASH_MARKERS = (
    "traceback (most recent call last):",
    "internal error",
    "unhandled exception",
    "segmentation fault",
    "core dumped",
    "usage: mypy",
    "mypy: error:",
    "fatal error",
    "syntaxerror:",
)


def normalize_line(line: str) -> str:
    cleaned = line.strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def classify_line(line: str) -> tuple[str, str]:
    """Classify a non-empty mypy output line into error, note, summary, crash, or unknown."""
    norm = normalize_line(line)
    lower = norm.lower()

    for marker in _CRASH_MARKERS:
        if marker in lower:
            return "CRASH", norm

    if _RE_ERROR.match(norm):
        return "ERROR", norm
    if _RE_NOTE.match(norm):
        return "NOTE", norm
    if _RE_SUMMARY.match(norm):
        return "SUMMARY", norm

    return "UNKNOWN", norm


def parse_baseline(baseline_text: str) -> list[str]:
    """Parse baseline error lines."""
    errors = []
    for line in baseline_text.splitlines():
        norm = normalize_line(line)
        if not norm or norm.startswith("#"):
            continue
        if _RE_ERROR.match(norm):
            errors.append(norm)
    return errors


def check_mypy_output(
    returncode: int,
    stdout: str,
    stderr: str,
    baseline_path: Path,
) -> int:
    """Evaluate mypy results against baseline with fail-closed completion semantics."""
    if not baseline_path.exists():
        print(f"\033[31m❌ mypy 基线文件不存在: {baseline_path}\033[0m", file=sys.stderr)
        return 1

    # 1. Reject unexpected exit codes (crashes, signals, usage errors)
    if returncode not in (0, 1):
        print(
            f"\033[31m❌ mypy 进程非正常退出 (退出码 {returncode}，判定为工具崩溃或运行环境异常，门禁阻断):\033[0m",
            file=sys.stderr,
        )
        if stderr.strip():
            print(f"STDERR:\n{stderr.strip()}", file=sys.stderr)
        if stdout.strip():
            print(f"STDOUT:\n{stdout.strip()}", file=sys.stderr)
        return 1

    # 2. Parse and classify every output line from stdout and stderr
    all_lines: list[tuple[str, str, str]] = []  # (stream, category, content)

    for line in stdout.splitlines():
        if line.strip():
            cat, norm = classify_line(line)
            all_lines.append(("stdout", cat, norm))

    for line in stderr.splitlines():
        if line.strip():
            cat, norm = classify_line(line)
            all_lines.append(("stderr", cat, norm))

    crash_lines = [item for item in all_lines if item[1] == "CRASH"]
    unknown_lines = [item for item in all_lines if item[1] == "UNKNOWN"]
    actual_errors = [item[2] for item in all_lines if item[1] == "ERROR"]

    # 3. Fail closed if crash markers or unparseable output lines are present
    if crash_lines:
        print(
            f"\033[31m❌ mypy 输出包含异常/崩溃信息 (共 {len(crash_lines)} 行，门禁阻断):\033[0m",
            file=sys.stderr,
        )
        for stream, _, text in crash_lines:
            print(f"  [{stream}] \033[31m{text}\033[0m", file=sys.stderr)
        return 1

    if unknown_lines:
        print(
            f"\033[31m❌ mypy 输出包含无法识别的内容 (共 {len(unknown_lines)} 行，疑似异常截断，门禁阻断):\033[0m",
            file=sys.stderr,
        )
        for stream, _, text in unknown_lines:
            print(f"  [{stream}] \033[31m{text}\033[0m", file=sys.stderr)
        return 1

    # 4. Fail closed on returncode 0 with error diagnostics
    if returncode == 0 and actual_errors:
        print(
            f"\033[31m❌ mypy 退出码为 0 但输出了 {len(actual_errors)} 条错误诊断 (异常状态，门禁阻断):\033[0m",
            file=sys.stderr,
        )
        for err in actual_errors:
            print(f"  \033[31m• {err}\033[0m", file=sys.stderr)
        return 1

    # 5. Fail closed on returncode 1 with zero error diagnostics
    if returncode == 1 and not actual_errors:
        print(
            "\033[31m❌ mypy 退出码为 1 (存在失败) 但未输出任何标准 ': error:' 诊断 (门禁阻断):\033[0m",
            file=sys.stderr,
        )
        return 1

    baseline_errors = parse_baseline(baseline_path.read_text(encoding="utf-8"))
    baseline_set = set(baseline_errors)
    actual_set = set(actual_errors)

    new_errors = [err for err in actual_errors if err not in baseline_set]
    fixed_errors = [err for err in baseline_errors if err not in actual_set]

    # 6. Reject any error not in baseline
    if new_errors:
        print(
            f"\033[31m❌ mypy 发现新增错误 (共 {len(new_errors)} 条，门禁阻断):\033[0m",
            file=sys.stderr,
        )
        for err in new_errors:
            print(f"  \033[31m+ {err}\033[0m", file=sys.stderr)
        return 1

    # 7. Success reporting
    if actual_errors:
        print(
            f"\033[33m⚠️  mypy 存在已知基线错误 (共 {len(actual_errors)} 条，已记录在基线文件):\033[0m"
        )
        for err in actual_errors:
            print(f"  \033[33m• {err}\033[0m")
        print("\033[32m✅ 无新增 mypy 错误 (基线精确匹配通过)\033[0m")
    else:
        print("\033[32m✅ mypy 零错误通过\033[0m")

    if fixed_errors:
        print(f"\033[36mℹ️ 已修复 {len(fixed_errors)} 条已知基线错误，可更新基线文件。\033[0m")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict mypy output validator against baseline.")
    parser.add_argument("--mypy-bin", default="./venv/bin/mypy", help="Path to mypy executable")
    parser.add_argument(
        "--baseline-file",
        default="scripts/test_safety_mypy_baseline.txt",
        help="Path to baseline errors file",
    )
    args = parser.parse_args()

    baseline_path = Path(args.baseline_file)
    proc = subprocess.run(
        [args.mypy_bin, "."],
        capture_output=True,
        text=True,
        check=False,
    )
    return check_mypy_output(proc.returncode, proc.stdout, proc.stderr, baseline_path)


if __name__ == "__main__":
    sys.exit(main())
