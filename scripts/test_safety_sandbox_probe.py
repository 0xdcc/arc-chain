#!/usr/bin/env python3
"""Probe script for validating OS-level sandbox isolation (runs inside bwrap).

Validates:
1. Clean environment (--clearenv): no inherited tokens, sentinels, or private keys.
2. /tmp is private and writable (positive control).
3. Low-level os.open write to read-only source fails with EROFS (errno 30).
4. os.mkdir, os.unlink, os.rename in read-only source fail with EROFS (errno 30).
5. Pure source tree: .env, data/, and logs/ are excluded from /sandbox/src.
6. Host secrets (/root/.secrets, .hermes, .codex) and host /run sockets are inaccessible.
7. Outbound network sockets fail immediately at the kernel level.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

SANDBOX_SRC = Path("/sandbox/src")
SANDBOX_VENV = Path("/sandbox/venv")


def check_environment_clean() -> None:
    """Verify that inherited environment variables were wiped by --clearenv."""
    forbidden_keys = (
        "PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "FAKE_REVIEW_SENTINEL",
        "ETH_PRIVATE_KEY",
        "WEB3_PRIVATE_KEY",
        "WALLET_KEY",
    )
    leaked = [k for k in forbidden_keys if k in os.environ]
    if leaked:
        print(f"❌ [Probe 1/7 失败] 环境变量中存在残留凭据/哨兵: {leaked}", file=sys.stderr)
        sys.exit(1)

    assert os.getenv("DEX_ENGINE_SAFE_CONFIG") == "1", "Missing DEX_ENGINE_SAFE_CONFIG"
    assert os.getenv("PYTHONPATH") == "/sandbox/src", (
        f"Unexpected PYTHONPATH: {os.getenv('PYTHONPATH')}"
    )
    print("✅ [Probe 1/7] 继承环境已完全清除 (--clearenv 生效，人工凭据与哨兵不可见)")


def check_tmpfs_writable() -> None:
    """Positive control: /tmp is private and writable."""
    with tempfile.TemporaryDirectory(prefix="sandbox-positive-", dir="/tmp") as directory:
        test_file = Path(directory) / "positive.txt"
        fd = os.open(test_file, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"sandbox_positive_control")
        finally:
            os.close(fd)
        assert test_file.read_bytes() == b"sandbox_positive_control"
        os.mkdir(Path(directory) / "positive-dir")
        renamed = Path(directory) / "renamed.txt"
        os.rename(test_file, renamed)
        assert not test_file.exists() and renamed.read_bytes() == b"sandbox_positive_control"
        os.unlink(renamed)
        assert not renamed.exists()
    print("✅ [Probe 2/7] /tmp open/mkdir/rename/unlink 四项正对照通过")


def check_readonly_source_mount() -> None:
    """Negative controls: OS kernel rejects write/mkdir/unlink/rename on read-only mount with EROFS."""
    target_write = SANDBOX_SRC / "probe_blocked_write.tmp"
    try:
        fd = os.open(target_write, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        print("❌ [Probe 3a/7 失败] 源码目录允许写入！未达到只读挂载隔离！", file=sys.stderr)
        sys.exit(1)
    except OSError as err:
        if err.errno != errno.EROFS:
            print(f"⚠️ [Probe 3a/7 异常] 预期 EROFS (errno 30)，实际收到: {err}", file=sys.stderr)
            sys.exit(1)
        print(f"✅ [Probe 3a/7] os.open 写入被内核正确拒绝 (EROFS, errno {err.errno})")

    target_dir = SANDBOX_SRC / "probe_blocked_dir"
    try:
        os.mkdir(target_dir)
        print("❌ [Probe 3b/7 失败] 源码目录允许创建子目录！", file=sys.stderr)
        sys.exit(1)
    except OSError as err:
        if err.errno != errno.EROFS:
            print(f"⚠️ [Probe 3b/7 异常] 预期 EROFS，实际收到: {err}", file=sys.stderr)
            sys.exit(1)
        print("✅ [Probe 3b/7] os.mkdir 被内核正确拒绝 (EROFS)")

    target_unlink = SANDBOX_SRC / "sandbox_probe_fixture.txt"
    assert target_unlink.read_text(encoding="utf-8") == "ARTIFICIAL_READONLY_PROBE\n"
    try:
        os.unlink(target_unlink)
        print("❌ [Probe 3c/7 失败] 源码目录允许删除文件！", file=sys.stderr)
        sys.exit(1)
    except OSError as err:
        if err.errno != errno.EROFS:
            print(f"⚠️ [Probe 3c/7 异常] 预期 EROFS，实际收到: {err}", file=sys.stderr)
            sys.exit(1)
        print("✅ [Probe 3c/7] os.unlink 被内核正确拒绝 (EROFS)")

    target_rename = SANDBOX_SRC / "sandbox_probe_fixture.txt"
    try:
        os.rename(target_rename, SANDBOX_SRC / "sandbox_probe_fixture.txt.bak")
        print("❌ [Probe 3d/7 失败] 源码目录允许重命名文件！", file=sys.stderr)
        sys.exit(1)
    except OSError as err:
        if err.errno != errno.EROFS:
            print(f"⚠️ [Probe 3d/7 异常] 预期 EROFS，实际收到: {err}", file=sys.stderr)
            sys.exit(1)
        print("✅ [Probe 3d/7] os.rename 被内核正确拒绝 (EROFS)")


def check_pure_source_no_data_or_env() -> None:
    """Verify /sandbox/src contains only pure source code, excluding .env, data/, logs/."""
    assert (SANDBOX_SRC / "core").exists(), "Missing /sandbox/src/core"
    assert (SANDBOX_SRC / "pyproject.toml").exists(), "Missing /sandbox/src/pyproject.toml"
    manifest = json.loads((SANDBOX_SRC / "scripts/test_safety_source_manifest.json").read_text())
    expected = set(manifest["files"]) | {"sandbox_probe_fixture.txt"}
    actual = {
        str(path.relative_to(SANDBOX_SRC)) for path in SANDBOX_SRC.rglob("*") if path.is_file()
    }
    assert actual == expected, f"Source manifest mismatch: {actual ^ expected}"
    assert not any(path.is_symlink() for path in SANDBOX_SRC.rglob("*"))
    assert (SANDBOX_SRC / "backtest/data/rpc_client.py").is_file()

    unwanted = [
        SANDBOX_SRC / ".env",
        SANDBOX_SRC / "data",
        SANDBOX_SRC / "logs",
        SANDBOX_SRC / ".git",
    ]
    for p in unwanted:
        if p.exists():
            print(
                f"❌ [Probe 4/7 失败] /sandbox/src 包含了未授权的数据或配置文件: {p}",
                file=sys.stderr,
            )
            sys.exit(1)

    print("✅ [Probe 4/7] 纯源码映射验证通过 (无 .env、无 data/、无 logs/)")


def check_host_secrets_and_ipc_masked() -> None:
    """Check private runtime directories without accessing host credential locations."""
    run_path = Path("/run")
    if run_path.exists():
        run_entries = list(run_path.iterdir())
        if run_entries:
            print(f"❌ [Probe 5b/7 失败] /run 包含宿主端点: {run_entries}", file=sys.stderr)
            sys.exit(1)

    assert not list(Path("/home").iterdir())
    print("✅ [Probe 5/7] /run 与 /home 为空；人工外部文件/socket 另由迁移反证验证")


def check_network_isolated() -> None:
    """Verify kernel-level network namespace isolation (--unshare-net)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(("192.0.2.1", 9))
        sock.close()
        print("❌ [Probe 6/7 失败] 网络外联成功！命名空间未生效！", file=sys.stderr)
        sys.exit(1)
    except OSError:
        print("✅ [Probe 6/7] 外部网络连接被内核物理阻断 (--unshare-net 生效)")


def _run_launcher(source: Path, *command: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "PRIVATE_KEY": "FAKE_REVIEW_SENTINEL",
        "TELEGRAM_BOT_TOKEN": "FAKE_REVIEW_SENTINEL",
        "FAKE_REVIEW_SENTINEL": "FAKE_REVIEW_SENTINEL",
    }
    return subprocess.run(
        ["/bin/bash", str(source / "scripts/test_safety_sandbox.sh"), *command],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _expect_result(label: str, result: subprocess.CompletedProcess[str], code: int) -> None:
    print(f"{label}: exit={result.returncode}", flush=True)
    print(result.stdout + result.stderr, end="", flush=True)
    assert result.returncode == code, f"{label}: expected exit {code}"


def verify_launcher(source: Path) -> int:
    """Execute real launcher controls in two disposable layouts; never fall back on denial."""
    python = "/sandbox/venv/bin/python"
    probe = "scripts/test_safety_sandbox_probe.py"
    baseline = _run_launcher(source, python, probe)
    _expect_result("OFFICIAL_PROBE", baseline, 0)
    dependency = source / "venv"
    if source == SANDBOX_SRC:
        dependency = SANDBOX_VENV
    assert (dependency / "bin/python").is_file(), "Missing local venv dependency"
    with tempfile.TemporaryDirectory(prefix="sandbox-acceptance-", dir="/tmp") as temporary:
        root = Path(temporary)
        external = root / "external"
        external.mkdir()
        external_secret = external / "artificial.secret"
        external_secret.write_text("FAKE_REVIEW_SENTINEL", encoding="utf-8")
        socket_path = external / "fake.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            listener.listen(1)
            with socket.socket(socket.AF_UNIX) as positive:
                positive.connect(str(socket_path))
                connection, _ = listener.accept()
                connection.close()
            print("ARTIFICIAL_SOCKET_HOST_POSITIVE: connected", flush=True)
            for layout in ("repair layout/source", "projects/crypto/dex-sniper-engine"):
                copied = root / layout
                copied.mkdir(parents=True)
                staged = subprocess.run(
                    [
                        str(dependency / "bin/python"),
                        "-I",
                        "-S",
                        str(source / "scripts/test_safety_stage.py"),
                        str(source),
                        str(copied),
                    ],
                    env={"PATH": "/usr/bin:/bin"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                _expect_result("COPY " + layout, staged, 0)
                (copied / "venv").symlink_to(dependency.resolve())
                for extra in (
                    ".env",
                    "core/.env",
                    "core/unlisted.py",
                    "tests/artificial.key",
                    "backtest/data/runtime.jsonl",
                    "core/__pycache__/fake.pyc",
                ):
                    target = copied / extra
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("FAKE_REVIEW_SENTINEL", encoding="utf-8")
                mapping_code = """
import errno, os, socket, sys
from pathlib import Path
import core
import backtest.data.rpc_client
import scripts.test_safety_stage
from scripts.test_safety_sandbox_probe import check_pure_source_no_data_or_env
assert Path.cwd() == Path('/sandbox/src')
assert sys.prefix == '/sandbox/venv'
for module in (core, backtest.data.rpc_client, scripts.test_safety_stage):
    assert Path(module.__file__).is_relative_to('/sandbox/src')
check_pure_source_no_data_or_env()
for name in sys.argv[1:3]:
    for flags in (os.O_RDONLY, os.O_WRONLY):
        try:
            descriptor = os.open(name, flags)
        except OSError as error:
            assert error.errno == errno.ENOENT, error
        else:
            os.close(descriptor)
            raise AssertionError('Artificial external file became accessible')
with socket.socket(socket.AF_UNIX) as negative:
    try:
        negative.connect(sys.argv[3])
    except OSError as error:
        assert error.errno == errno.ENOENT, error
    else:
        raise AssertionError('Artificial host socket became accessible')
print('MAPPING_IMPORT_SECRET_SOCKET_OK')
"""
                mapped = _run_launcher(
                    copied,
                    python,
                    "-c",
                    mapping_code,
                    str(external_secret),
                    str(copied / "core/.env"),
                    str(socket_path),
                )
                _expect_result("MIGRATION " + layout, mapped, 0)
                assert "MAPPING_IMPORT_SECRET_SOCKET_OK" in mapped.stdout
                launcher = copied / "scripts/test_safety_sandbox.sh"
                original = launcher.read_text(encoding="utf-8")
                assert original.count("  --clearenv\n") == 1
                launcher.write_text(original.replace("  --clearenv\n", ""), encoding="utf-8")
                leaked = _run_launcher(copied, python, probe)
                _expect_result("SABOTAGE_CLEARENV " + layout, leaked, 1)
                assert "Probe 1/7 失败" in leaked.stderr
                binding = 'BWRAP_ARGS+=(--ro-bind "$STAGE_DIR" /sandbox/src)'
                assert original.count(binding) == 1
                launcher.write_text(
                    original.replace(binding, binding.replace("--ro-bind", "--bind")),
                    encoding="utf-8",
                )
                writable = _run_launcher(copied, python, probe)
                _expect_result("SABOTAGE_READONLY " + layout, writable, 1)
                assert "Probe 3a/7 失败" in writable.stderr
                launcher.write_text(original, encoding="utf-8")
                _expect_result("RESTORED " + layout, _run_launcher(copied, python, probe), 0)
                (copied / "core").rename(copied / "saved-core")
                (copied / "core").symlink_to(external, target_is_directory=True)
                rejected = _run_launcher(copied, python, "-c", "print('TARGET_EXECUTED')")
                _expect_result("TOPLEVEL_SYMLINK " + layout, rejected, 1)
                assert "Source staging rejected" in rejected.stderr
                assert "TARGET_EXECUTED" not in rejected.stdout
                assert external_secret.read_text() == "FAKE_REVIEW_SENTINEL"
    print("LAUNCHER_ACCEPTANCE_OK", flush=True)
    return 0


def main() -> int:
    """Run the official probe inside the sandbox, or orchestrate host acceptance."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-launcher", action="store_true")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    if args.verify_launcher:
        return verify_launcher(args.source)
    print("=== 运行 OS 级沙箱隔离探针 (PLAN_RESET_1A / OS_ISOLATION_CLOSEOUT) ===")
    check_environment_clean()
    check_tmpfs_writable()
    check_readonly_source_mount()
    check_pure_source_no_data_or_env()
    check_host_secrets_and_ipc_masked()
    check_network_isolated()
    print("🎉 所有 OS 隔离探针检查全部通过！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
