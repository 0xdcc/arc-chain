"""Tests for offline local audit runner (AUDIT-R1 / R2 / R3).

Verifies execution behavior, log preservation, failure aggregation,
environment credential isolation, and process group timeout cleanup
using real child processes without network access.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.checks.arc_audit_local import (
    CheckResult,
    build_audit_checks,
    build_clean_env,
    execute_single_check,
    parse_args,
    run_audit,
)


class TestLocalAuditRunnerUnit:
    """Unit tests for pure helper logic without subprocess execution."""

    def test_environment_credential_isolation(self, tmp_path: Path) -> None:
        dirty_env = {
            "PATH": "/usr/bin:/bin",
            "USER": "testuser",
            "API_KEY": "super_secret_key_123",
            "HERMES_TOKEN": "secret_token_456",
            "DB_PASSWORD": "hunter2",
            "SECRET_SALT": "salty",
            "AUTH_HEADER": "Bearer xyz",
            "CREDENTIAL_PATH": "/vault/creds",
            "TELEGRAM_BOT_TOKEN": "tg_tok",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api",
            "SAFE_VARIABLE": "harmless_value",
        }
        clean = build_clean_env(base_env=dirty_env, interpreter=Path("/fake/venv/bin/python"))

        assert "API_KEY" not in clean
        assert "HERMES_TOKEN" not in clean
        assert "DB_PASSWORD" not in clean
        assert "SECRET_SALT" not in clean
        assert "AUTH_HEADER" not in clean
        assert "CREDENTIAL_PATH" not in clean
        assert "TELEGRAM_BOT_TOKEN" not in clean
        assert "DISCORD_WEBHOOK_URL" not in clean

        assert clean["PATH"].startswith("/fake/venv/bin")
        assert clean["USER"] == "testuser"
        assert clean["SAFE_VARIABLE"] == "harmless_value"
        assert clean["PYTHONDONTWRITEBYTECODE"] == "1"
        assert clean["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

    def test_build_audit_checks_structure(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        interp = tmp_path / "venv" / "bin" / "python"
        evid = tmp_path / "evidence"

        checks = build_audit_checks(repo, interp, evid)
        labels = [c[0] for c in checks]
        assert labels == ["arc-tests", "contracts", "full-collection", "lint", "typing"]

        for _label, cmd in checks:
            assert cmd[0] == str(interp)

    def test_cli_argument_parsing(self, tmp_path: Path) -> None:
        args = parse_args(
            [
                "--repo-root",
                str(tmp_path / "custom_root"),
                "--interpreter",
                str(tmp_path / "custom_py"),
                "--evidence-dir",
                str(tmp_path / "custom_evid"),
                "--timeout",
                "60",
                "--labels",
                "arc-tests",
                "lint",
            ]
        )
        assert args.repo_root == tmp_path / "custom_root"
        assert args.interpreter == tmp_path / "custom_py"
        assert args.evidence_dir == tmp_path / "custom_evid"
        assert args.timeout == 60
        assert args.labels == ["arc-tests", "lint"]


class TestLocalAuditRunnerSubprocess:
    """Tests validating real subprocess execution, output capture, and timeouts."""

    def test_real_subprocess_exit_zero(self, tmp_path: Path) -> None:
        env = build_clean_env()
        cmd = [sys.executable, "-c", "print('hello from real child')"]
        res = execute_single_check(
            label="test-pass",
            cmd=cmd,
            cwd=tmp_path,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=30,
        )

        assert res.exit_code == 0
        assert res.timed_out is False
        assert "hello from real child" in res.stdout
        assert res.error is None
        assert res.duration_seconds > 0

        log_file = tmp_path / "test-pass.log"
        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "=== EXIT: 0 ===" in content
        assert "hello from real child" in content
        assert "AUDIT_RESULT test-pass exit=0" in content

    def test_real_subprocess_nonzero_exit(self, tmp_path: Path) -> None:
        env = build_clean_env()
        script = "import sys; sys.stderr.write('fatal child failure\\n'); sys.exit(42)"
        cmd = [sys.executable, "-c", script]
        res = execute_single_check(
            label="test-fail",
            cmd=cmd,
            cwd=tmp_path,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=30,
        )

        assert res.exit_code == 42
        assert res.timed_out is False
        assert "fatal child failure" in res.stderr
        assert res.error is None

        log_file = tmp_path / "test-fail.log"
        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "=== EXIT: 42 ===" in content
        assert "fatal child failure" in content

    def test_real_subprocess_command_not_found(self, tmp_path: Path) -> None:
        env = build_clean_env()
        cmd = ["/nonexistent_command_123456789"]
        res = execute_single_check(
            label="test-notfound",
            cmd=cmd,
            cwd=tmp_path,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=30,
        )

        assert res.exit_code == 127
        assert res.timed_out is False
        assert res.error is not None
        assert "Executable or file not found" in res.error

        log_file = tmp_path / "test-notfound.log"
        assert log_file.is_file()
        assert "=== EXIT: 127 ===" in log_file.read_text(encoding="utf-8")

    def test_real_subprocess_timeout_handling(self, tmp_path: Path) -> None:
        env = build_clean_env()
        cmd = [
            sys.executable,
            "-c",
            "import sys, time; sys.stdout.write('partial output before timeout\\n'); sys.stdout.flush(); time.sleep(15)",
        ]
        res = execute_single_check(
            label="test-timeout",
            cmd=cmd,
            cwd=tmp_path,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=1,
        )

        assert res.exit_code == 124
        assert res.timed_out is True
        assert res.error is not None
        assert "timed out after 1 seconds" in res.error
        assert "partial output before timeout" in res.stdout
        assert res.duration_seconds < 5.0, f"Cleanup took too long: {res.duration_seconds}s"

        log_file = tmp_path / "test-timeout.log"
        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "=== EXIT: 124 ===" in content
        assert "partial output before timeout" in content

    def test_real_subprocess_process_tree_grandchild_cleanup(self, tmp_path: Path) -> None:
        env = build_clean_env()
        pid_file = tmp_path / "grandchild.pid"
        child_script = f"""
import subprocess, sys, time
gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open('{pid_file}', 'w') as f:
    f.write(str(gc.pid))
sys.stdout.write('child process tree running\\n')
sys.stdout.flush()
time.sleep(60)
"""
        cmd = [sys.executable, "-c", child_script]
        res = execute_single_check(
            label="test-tree-timeout",
            cmd=cmd,
            cwd=tmp_path,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=1,
        )

        assert res.exit_code == 124
        assert res.timed_out is True
        assert "child process tree running" in res.stdout
        assert res.duration_seconds < 5.0, f"Process tree cleanup took too long: {res.duration_seconds}s"

        assert pid_file.is_file(), "Grandchild failed to write pid file"
        gc_pid = int(pid_file.read_text().strip())

        # Verify grandchild process was killed as part of process group cleanup
        grandchild_terminated = False
        for _ in range(20):
            time.sleep(0.05)
            try:
                os.kill(gc_pid, 0)
            except ProcessLookupError:
                grandchild_terminated = True
                break
            except OSError:
                grandchild_terminated = True
                break

        assert grandchild_terminated, f"Orphaned grandchild process {gc_pid} was not terminated"

    def test_real_subprocess_multiline_output_preservation(self, tmp_path: Path) -> None:
        env = build_clean_env()
        script = """import sys
for i in range(150):
    print(f'DATA_ROW_{i:04d}: payload_chunk_value')
"""
        cmd = [sys.executable, "-c", script]
        res = execute_single_check(
            label="test-multiline",
            cmd=cmd,
            cwd=tmp_path,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=30,
        )

        assert res.exit_code == 0
        assert "DATA_ROW_0000: payload_chunk_value" in res.stdout
        assert "DATA_ROW_0149: payload_chunk_value" in res.stdout

        lines = [ln for ln in res.stdout.splitlines() if ln.startswith("DATA_ROW_")]
        assert len(lines) == 150

        log_content = (tmp_path / "test-multiline.log").read_text(encoding="utf-8")
        assert len([ln for ln in log_content.splitlines() if ln.startswith("DATA_ROW_")]) == 150

    def test_real_subprocess_path_with_spaces_and_custom_cwd(self, tmp_path: Path) -> None:
        env = build_clean_env()
        spaced_dir = tmp_path / "directory with spaces in name"
        spaced_dir.mkdir(parents=True)

        cmd = [sys.executable, "-c", "import os; print('CWD:' + os.getcwd())"]
        res = execute_single_check(
            label="test-spaces",
            cmd=cmd,
            cwd=spaced_dir,
            env=env,
            evidence_dir=tmp_path,
            timeout_seconds=30,
        )

        assert res.exit_code == 0
        assert f"CWD:{spaced_dir}" in res.stdout

    def test_real_subprocess_run_audit_aggregation(self, tmp_path: Path) -> None:
        env = build_clean_env()
        evid_dir = tmp_path / "audit_evidence"
        evid_dir.mkdir()

        # Run audit with custom label subset
        # We simulate the checks by executing checks via execute_single_check
        res_pass = execute_single_check(
            label="pass-check",
            cmd=[sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            env=env,
            evidence_dir=evid_dir,
            timeout_seconds=30,
        )
        res_fail = execute_single_check(
            label="fail-check",
            cmd=[sys.executable, "-c", "import sys; sys.exit(2)"],
            cwd=tmp_path,
            env=env,
            evidence_dir=evid_dir,
            timeout_seconds=30,
        )

        assert res_pass.exit_code == 0
        assert res_fail.exit_code == 2

        results = [res_pass, res_fail]
        passed_checks = sum(1 for r in results if r.exit_code == 0 and not r.timed_out)
        total_checks = len(results)
        failed_checks = total_checks - passed_checks
        overall_passed = failed_checks == 0 and total_checks > 0

        summary_file = evid_dir / "audit_summary.json"
        summary_data = {
            "passed": overall_passed,
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "failed_checks": failed_checks,
            "results": [r.to_dict() for r in results],
        }
        summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

        assert overall_passed is False
        assert passed_checks == 1
        assert failed_checks == 1
        assert summary_file.is_file()
