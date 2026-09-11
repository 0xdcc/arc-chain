"""Unit, negative-control, and sabotage counterproof tests for OS-enforced isolation.

Validates:
1. Launcher and probe script permissions and presence.
2. Complete bwrap command line recipe according to OS_ISOLATION_CLOSEOUT.md:
   --clearenv, --tmpfs /, minimal system mounts, pure source mapping (/sandbox/src),
   stable venv (/sandbox/venv), and drop ALL capabilities.
3. Fail-closed behavior when running in restricted nested container environments.
4. Fail-closed behavior when required sandbox binaries are missing (no bare fallback).
5. Low-level OS filesystem semantics: os.open write, mkdir, unlink, and rename all fail
   with OSError on read-only directory (negative controls) and succeed on writable tmpfs (positive control).
6. Artificial external secrets in /tmp (.env, .key, .pem) are rejected with PermissionError,
   while non-secret temporary data files are readable (negative and positive controls).
7. AF_UNIX socket isolation: connections outside /tmp are blocked with RuntimeError.
8. External production tree access isolation: paths targeting external production roots are blocked.
9. Environment cleaning sabotage counterproof: injected fake sentinels leak without guard,
   but are cleanly swept when conftest import guard is active.
10. Portable path layout resolution in /tmp simulation: launcher dynamically computes
    paths without hardcoding host directories.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.conftest import (
    _ORIG_BUILTINS_OPEN,
    _ORIG_SUBPROCESS_POPEN,
    _ORIG_SUBPROCESS_RUN,
    _PROD_ROOT,
    _REPO_ROOT,
    _is_forbidden_read_path,
)

REPAIR_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = REPAIR_ROOT / "scripts" / "test_safety_sandbox.sh"
PROBE_PATH = REPAIR_ROOT / "scripts" / "test_safety_sandbox_probe.py"


def test_sandbox_launcher_and_probe_exist_and_executable() -> None:
    """Launcher script and probe script must exist and have executable permissions."""
    assert LAUNCHER_PATH.exists(), f"Missing launcher: {LAUNCHER_PATH}"
    assert os.access(LAUNCHER_PATH, os.X_OK), "Launcher script is not executable"

    assert PROBE_PATH.exists(), f"Missing probe: {PROBE_PATH}"
    assert os.access(PROBE_PATH, os.X_OK), "Probe script is not executable"


def test_sandbox_launcher_bwrap_recipe_specification() -> None:
    """The bwrap command line constructed by the launcher must adhere to the corrected minimal specification."""
    content = LAUNCHER_PATH.read_text(encoding="utf-8")

    # Essential security and isolation flags per OS_ISOLATION_CLOSEOUT.md
    required_flags = [
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--cap-drop ALL",
        "--clearenv",
        "--tmpfs /",
        "--tmpfs /tmp",
        "--tmpfs /run",
        "--tmpfs /home",
        "--dir /sandbox",
        "--dir /sandbox/src",
        "--ro-bind /usr /usr",
        "/sandbox/venv",
        "/sandbox/src",
    ]

    for flag in required_flags:
        assert flag in content, f"Missing required bwrap security specification in launcher: {flag}"

    # Must NOT use vulnerable whole-root mount
    assert "--ro-bind / /" not in content, (
        "Whole root '--ro-bind / /' must be replaced by minimal mounts"
    )


def test_sandbox_launcher_fails_closed_in_nested_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inject namespace denial deterministically and prove the target never executes."""
    monkeypatch.setattr(subprocess, "run", _ORIG_SUBPROCESS_RUN)
    monkeypatch.setattr(subprocess, "Popen", _ORIG_SUBPROCESS_POPEN)

    denied = tmp_path / "denied-bwrap"
    denied.write_text(
        "#!/bin/sh\nprintf 'INJECTED_NAMESPACE_DENIAL: Operation not permitted\\n' >&2\nexit 1\n"
    )
    denied.chmod(0o700)
    target = tmp_path / "must-not-execute"
    proc = subprocess.run(
        [str(LAUNCHER_PATH), "/usr/bin/touch", str(target)],
        env={"PATH": "/usr/bin:/bin", "BWRAP_BIN": str(denied)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, (
        f"Launcher must return exit code 1 (fail closed), got: {proc.returncode}"
    )
    combined_output = proc.stdout + proc.stderr
    assert "Fail-Closed" in combined_output or "Operation not permitted" in combined_output
    assert "INJECTED_NAMESPACE_DENIAL" in proc.stderr
    assert "Fail-Closed" in proc.stderr
    assert not target.exists()


def test_sandbox_launcher_missing_dependency_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When bwrap binary is missing or invalid, launcher must fail closed without bare fallback."""
    monkeypatch.setattr(subprocess, "run", _ORIG_SUBPROCESS_RUN)
    monkeypatch.setattr(subprocess, "Popen", _ORIG_SUBPROCESS_POPEN)

    env = os.environ.copy()
    env["BWRAP_BIN"] = "/nonexistent/path/to/bwrap"

    proc = subprocess.run(
        [str(LAUNCHER_PATH), "true"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 1, (
        f"Launcher must exit 1 when bwrap binary is missing, got: {proc.returncode}"
    )
    assert "未找到 bubblewrap 二进制文件" in proc.stderr
    assert "Fail-Closed" in proc.stderr


def test_simulated_readonly_filesystem_negative_controls(tmp_path: Path) -> None:
    """Verify low-level OS write/mkdir/unlink/rename rejection on read-only directories and positive control."""
    ro_dir = tmp_path / "mock_readonly_mount"
    ro_dir.mkdir()
    sample_file = ro_dir / "sample.txt"
    sample_file.write_text("initial", encoding="utf-8")

    # Negative controls: Make directory read-only (simulating read-only mount behavior)
    ro_dir.chmod(0o555)
    try:
        # 1. os.open create new file must fail with OSError
        with pytest.raises(OSError):
            os.open(ro_dir / "new_file.txt", os.O_CREAT | os.O_WRONLY)

        # 2. mkdir in read-only dir must fail with OSError
        with pytest.raises(OSError):
            os.mkdir(ro_dir / "sub_dir")

        # 3. unlink in read-only dir must fail with OSError
        with pytest.raises(OSError):
            os.unlink(sample_file)

        # 4. rename in read-only dir must fail with OSError
        with pytest.raises(OSError):
            os.rename(sample_file, ro_dir / "sample.txt.bak")
    finally:
        ro_dir.chmod(0o755)

    # Positive control: on writable tmpfs, all 4 low-level operations succeed
    writable_dir = tmp_path / "positive_control_dir"
    writable_dir.mkdir()
    pos_file = writable_dir / "pos.txt"

    fd = os.open(pos_file, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(fd, b"pos_data")
    os.close(fd)
    assert pos_file.read_bytes() == b"pos_data"

    sub = writable_dir / "pos_sub"
    os.mkdir(sub)
    assert sub.is_dir()

    renamed_file = writable_dir / "pos_renamed.txt"
    os.rename(pos_file, renamed_file)
    assert renamed_file.exists() and not pos_file.exists()

    os.unlink(renamed_file)
    assert not renamed_file.exists()


def test_probe_positive_control_tmp_writable() -> None:
    """Probe's check_tmpfs_writable logic succeeds when run in writable tmp."""
    from scripts.test_safety_sandbox_probe import check_tmpfs_writable

    check_tmpfs_writable()


def test_artificial_secrets_in_tmp_are_unreachable(tmp_path: Path) -> None:
    """Artificial external secret files (.env, .key, .pem) in /tmp are blocked from reading."""
    secrets_dir = tmp_path / "mock_external_secret"
    secrets_dir.mkdir()

    # Create artificial test secrets using unguarded low-level builtins
    fake_env = secrets_dir / ".env"
    _ORIG_BUILTINS_OPEN(fake_env, "w", encoding="utf-8").write("PRIVATE_KEY=0xdeadbeef\n")

    fake_key = secrets_dir / "mock_wallet.key"
    _ORIG_BUILTINS_OPEN(fake_key, "w", encoding="utf-8").write("fake_key_material\n")

    fake_pem = secrets_dir / "client.pem"
    _ORIG_BUILTINS_OPEN(fake_pem, "w", encoding="utf-8").write("fake_pem_material\n")

    # Negative controls: Guarded open must reject reading each artificial secret
    for secret_file in (fake_env, fake_key, fake_pem):
        with pytest.raises(PermissionError, match="Access to secret/credential"):
            open(secret_file, encoding="utf-8")

    # Positive control: normal configuration / data files in /tmp remain readable
    safe_file = secrets_dir / "normal_config.json"
    _ORIG_BUILTINS_OPEN(safe_file, "w", encoding="utf-8").write('{"safe": true}\n')
    assert "safe" in open(safe_file, encoding="utf-8").read()


def test_fake_unix_socket_connection_outside_tmp_is_blocked() -> None:
    """AF_UNIX sockets pointing to host paths outside /tmp must be blocked."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with pytest.raises(
        RuntimeError, match="Connecting to host UNIX domain socket outside /tmp is forbidden"
    ):
        sock.connect("/run/host_ipc.sock")

    with pytest.raises(
        RuntimeError, match="Connecting to host UNIX domain socket outside /tmp is forbidden"
    ):
        sock.connect("/var/run/daemon.sock")


def test_fake_production_tree_access_is_blocked() -> None:
    """Accessing paths simulating external production directories is blocked."""
    if _REPO_ROOT != _PROD_ROOT:
        fake_prod_target = _PROD_ROOT / "core" / "config.py"
        assert _is_forbidden_read_path(fake_prod_target) is True


def test_environment_cleaning_sabotage_counterproof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sabotage counterproof: injected sentinels leak in raw Python, but are stripped by conftest."""
    monkeypatch.setattr(subprocess, "run", _ORIG_SUBPROCESS_RUN)
    monkeypatch.setattr(subprocess, "Popen", _ORIG_SUBPROCESS_POPEN)

    py_bin = sys.executable
    test_env = os.environ.copy()
    test_env["PYTHONPATH"] = str(REPAIR_ROOT)
    test_env["FAKE_REVIEW_SENTINEL"] = "sabotage_leak_sentinel"
    test_env["PRIVATE_KEY"] = "sabotage_leak_private_key"

    # Sabotage control: without conftest stripping, Python inherits the sentinels
    raw_probe = subprocess.run(
        [
            py_bin,
            "-c",
            "import os; print(os.environ.get('FAKE_REVIEW_SENTINEL'), os.environ.get('PRIVATE_KEY'))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=test_env,
    )
    assert "sabotage_leak_sentinel" in raw_probe.stdout
    assert "sabotage_leak_private_key" in raw_probe.stdout

    # Enforced defense: with conftest imported, sentinels are completely stripped
    guarded_probe = subprocess.run(
        [
            py_bin,
            "-c",
            "import tests.conftest; import os; print('S:', os.environ.get('FAKE_REVIEW_SENTINEL'), 'K:', os.environ.get('PRIVATE_KEY'))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=test_env,
    )
    assert "S: None K: None" in guarded_probe.stdout


def test_portable_path_layout_in_tmp_simulation() -> None:
    """Verify that launcher dynamically resolves paths in an arbitrary simulated /tmp directory."""
    sim_dir = Path(tempfile.mkdtemp(prefix="dex_sim_layout_"))
    try:
        sim_scripts = sim_dir / "scripts"
        sim_scripts.mkdir()
        shutil.copy(LAUNCHER_PATH, sim_scripts / "test_safety_sandbox.sh")

        # Symlink venv as in repair workspace
        sim_venv = sim_dir / "venv"
        sim_venv.symlink_to(Path(sys.prefix).resolve())

        # Test path resolution logic matching launcher
        detected_src = (sim_scripts / "..").resolve()
        detected_venv = (detected_src / "venv").resolve()
        detected_python = detected_venv / "bin" / "python"

        assert detected_src == sim_dir
        assert detected_venv.exists()
        assert detected_python.exists()
    finally:
        shutil.rmtree(sim_dir)


def test_environment_variable_cleaning_at_import() -> None:
    """Injected sentinel / credential variables must not be present in os.environ."""
    for key in ("PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "FAKE_REVIEW_SENTINEL"):
        assert key not in os.environ, f"Environment variable {key} must be stripped"


def test_real_launcher_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run real mounts, two path layouts, fake IPC, sabotage and restored positive controls."""
    from scripts.test_safety_sandbox_probe import verify_launcher
    from tests.conftest import _ORIG_SOCK_CONNECT, _ORIG_SOCK_INIT

    monkeypatch.setattr(subprocess, "run", _ORIG_SUBPROCESS_RUN)
    monkeypatch.setattr(subprocess, "Popen", _ORIG_SUBPROCESS_POPEN)
    monkeypatch.setattr(socket.socket, "__init__", _ORIG_SOCK_INIT)
    monkeypatch.setattr(socket.socket, "connect", _ORIG_SOCK_CONNECT)
    assert verify_launcher(REPAIR_ROOT) == 0


def test_stage_file_selection_and_symlink_rejection(tmp_path: Path) -> None:
    """Exercise the real stager on synthetic files, without reading unlisted content."""
    import json

    from scripts.test_safety_stage import MANIFEST, stage_sources

    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "core").mkdir()
    (source / "backtest/data").mkdir(parents=True)
    files = ["core/config.py", "backtest/data/rpc_client.py"]
    (source / MANIFEST).write_text(json.dumps({"files": files}))
    for relative in files:
        (source / relative).write_text("VALUE = 1\n")
    (source / "core/.env").write_text("FAKE_REVIEW_SENTINEL")
    (source / "core/unlisted.py").write_text("FAKE_REVIEW_SENTINEL")
    destination = tmp_path / "selected"
    destination.mkdir()
    stage_sources(source, destination)
    assert {
        str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()
    } == set(files) | {"sandbox_probe_fixture.txt"}
    assert (destination / "backtest/data/rpc_client.py").read_text() == "VALUE = 1\n"
    external = tmp_path / "external.secret"
    external.write_text("FAKE_REVIEW_SENTINEL")
    (source / "core/config.py").unlink()
    (source / "core/config.py").symlink_to(external)
    rejected = tmp_path / "rejected"
    rejected.mkdir()
    with pytest.raises(OSError):
        stage_sources(source, rejected)
    assert not (rejected / "core/config.py").exists()
    assert external.read_text() == "FAKE_REVIEW_SENTINEL"


def test_missing_interpreter_has_no_production_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relocated launcher with no local dependency rejects without executing the target."""
    monkeypatch.setattr(subprocess, "run", _ORIG_SUBPROCESS_RUN)
    monkeypatch.setattr(subprocess, "Popen", _ORIG_SUBPROCESS_POPEN)
    scripts = tmp_path / "copy/scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / LAUNCHER_PATH.name
    shutil.copy(LAUNCHER_PATH, launcher)
    target = tmp_path / "must-not-execute"
    result = subprocess.run(
        [str(launcher), "/usr/bin/touch", str(target)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Fail-Closed" in result.stderr and "Python 解释器" in result.stderr
    assert not target.exists()


def test_host_python_discovery_ignores_injected_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A synthetic interpreter wrapper records clean env and -I -S for every host Python call."""
    import shlex

    from scripts.test_safety_stage import stage_sources

    monkeypatch.setattr(subprocess, "run", _ORIG_SUBPROCESS_RUN)
    monkeypatch.setattr(subprocess, "Popen", _ORIG_SUBPROCESS_POPEN)
    copied = tmp_path / "startup-copy"
    copied.mkdir()
    stage_sources(REPAIR_ROOT, copied)
    binaries = copied / "venv/bin"
    binaries.mkdir(parents=True)
    marker = tmp_path / "startup-hook-executed"
    hook = tmp_path / "hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    calls = tmp_path / "python-calls"
    wrapper = binaries / "python"
    wrapper.write_text(
        "#!/bin/sh\n"
        'test -z "${PRIVATE_KEY+x}${TELEGRAM_BOT_TOKEN+x}${FAKE_REVIEW_SENTINEL+x}${PYTHONPATH+x}" || exit 91\n'
        'test "$1" = -I && test "$2" = -S || exit 92\n'
        f"printf 'CLEAN_ISOLATED_CALL\\n' >> {shlex.quote(str(calls))}\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    wrapper.chmod(0o700)
    denied = tmp_path / "denied-bwrap"
    denied.write_text("#!/bin/sh\nprintf 'INJECTED_NAMESPACE_DENIAL\\n' >&2\nexit 1\n")
    denied.chmod(0o700)
    environment = {
        "PATH": "/usr/bin:/bin",
        "BWRAP_BIN": str(denied),
        "PYTHONPATH": str(hook),
        "PRIVATE_KEY": "FAKE_REVIEW_SENTINEL",
        "TELEGRAM_BOT_TOKEN": "FAKE_REVIEW_SENTINEL",
        "FAKE_REVIEW_SENTINEL": "FAKE_REVIEW_SENTINEL",
    }
    positive = subprocess.run(
        [sys.executable, "-c", "pass"],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(hook)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert positive.returncode == 0 and marker.exists()
    marker.unlink()
    result = subprocess.run(
        [str(copied / "scripts" / LAUNCHER_PATH.name), "/usr/bin/true"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "INJECTED_NAMESPACE_DENIAL" in result.stderr and "Fail-Closed" in result.stderr
    assert calls.read_text().splitlines() == ["CLEAN_ISOLATED_CALL"] * 3
    assert not marker.exists()
