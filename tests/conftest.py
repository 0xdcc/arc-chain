"""Global pytest fixtures and safety isolation harness for Dex-Sniper-Engine tests."""

from __future__ import annotations

import builtins
import inspect
import io
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

# 1. Platform architecture mock at import time: prevents pycryptodome from spawning subprocesses
platform.architecture = lambda *args, **kwargs: ("64bit", "ELF")

# 2. Redirect pytest cache into /tmp by default to prevent workspace pollution
os.environ["PYTEST_ADDOPTS"] = "-o cache_dir=/tmp/.pytest_cache " + os.environ.get(
    "PYTEST_ADDOPTS", ""
)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["DEX_ENGINE_SAFE_CONFIG"] = "1"

# 3. Path references
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROD_ROOT = Path("/root/projects/crypto/dex-sniper-engine").resolve()
_PROD_VENV = Path("/root/projects/crypto/dex-sniper-engine/venv").resolve()
_SECRETS_DIR = Path("/root/.secrets").resolve()
_TMP_ROOT = Path("/tmp").resolve()
_COLLECTION_LOGS_DIR = Path(tempfile.mkdtemp(prefix="dex_engine_collection_logs_"))

# 4. Strip external production repository from sys.path to eliminate editable fallback risk
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _REPO_ROOT != _PROD_ROOT:
    sys.path = [p for p in sys.path if Path(p).resolve() != _PROD_ROOT]

# 5. Strip all sensitive private key / credential / external token / test sentinel environment variables
for _key in list(os.environ.keys()):
    _k_upper = _key.upper()
    if any(
        needle in _k_upper
        for needle in (
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
    ):
        os.environ.pop(_key, None)

import pytest  # noqa: E402

try:
    from core.config import load_safe_config, set_config  # noqa: E402
    # Initialize global singleton with isolated configuration (no .env)
    set_config(load_safe_config(_env_file=None, DRY_RUN=True))
except ImportError:
    # Arc-chain modular decoupling: core package is not imported into arc-chain
    pass

# Synthetic fixture for JSONL ingestion tests (written only inside isolated per-test tmp_path)
_SYNTHETIC_JSONL_OPPORTUNITY = json.dumps(
    {
        "_test_fixture": "synthetic_test_fixture_for_ingestion",
        "summary": "WETH -> USDG -> PONS -> WETH (Triangular)",
        "details": {
            "cycle": ["WETH", "USDG", "0x39dbed3a2bd333467115de45665cc57f813c4571", "WETH"],
            "legs": [
                {
                    "from_token": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
                    "to_token": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
                    "pool": {
                        "address": "0x1111111111111111111111111111111111111111",
                        "label": "WETH/USDG",
                        "fee_bps": 5.0,
                    },
                    "rate": 2500.0,
                    "fee_bps": 5.0,
                    "effective_rate": 2498.75,
                },
                {
                    "from_token": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
                    "to_token": "0x39dbed3a2bd333467115de45665cc57f813c4571",
                    "pool": {
                        "address": "0x2222222222222222222222222222222222222222",
                        "label": "USDG/PONS",
                        "fee_bps": 5.0,
                    },
                    "rate": 0.5,
                    "fee_bps": 5.0,
                    "effective_rate": 0.49975,
                },
                {
                    "from_token": "0x39dbed3a2bd333467115de45665cc57f813c4571",
                    "to_token": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
                    "pool": {
                        "address": "0x3333333333333333333333333333333333333333",
                        "label": "PONS/WETH",
                        "fee_bps": 5.0,
                    },
                    "rate": 0.00081,
                    "fee_bps": 5.0,
                    "effective_rate": 0.000809,
                },
            ],
            "max_profit_usd": 10.0,
            "profit_at_500u": 5.0,
            "gross_profit_pct": 1.2,
            "net_profit_pct": 1.0,
            "gross_multiplier": 1.012,
            "net_multiplier": 1.01,
            "fee_multiplier": 0.998,
            "expected_multiplier": 1.01,
        },
    },
    ensure_ascii=False,
)


def _is_secret_path(resolved: Path) -> bool:
    """Check if path targets secrets or unverified credential files outside /tmp."""
    if resolved.is_relative_to(_TMP_ROOT) and "mock_external_secret" not in resolved.parts:
        return False

    if resolved == _SECRETS_DIR or _SECRETS_DIR in resolved.parents:
        return True
    if resolved.name == ".env" or resolved.name.startswith(".env."):
        return True
    if any(p in (".hermes", ".codex") for p in resolved.parts):
        return True
    if resolved.suffix in (".pem", ".key") or "keystore" in resolved.name.lower():
        return True
    return False


def _is_forbidden_read_path(resolved: Path) -> bool:
    """Check if reading from this path is forbidden (secrets or production data)."""
    if _is_secret_path(resolved):
        return True

    # Reading external production source code or data outside venv is strictly forbidden
    if _REPO_ROOT != _PROD_ROOT and resolved.is_relative_to(_PROD_ROOT):
        try:
            if resolved.is_relative_to(_PROD_VENV):
                return False
        except ValueError:
            pass
        return True

    return False


def _is_forbidden_write_path(resolved: Path) -> bool:
    """Check if write targets persistent paths outside /tmp (strictly no cache whitelist)."""
    # Writes are allowed exclusively within /tmp
    if resolved.is_relative_to(_TMP_ROOT):
        return False

    # All writes outside /tmp are forbidden (production root, logs/, data/, .pytest_cache, etc.)
    return True


_ORIG_IO_OPEN = io.open
_ORIG_BUILTINS_OPEN = builtins.open
_ORIG_FILEHANDLER_INIT = logging.FileHandler.__init__
_ORIG_SOCK_INIT = socket.socket.__init__
_ORIG_SOCK_CONNECT = socket.socket.connect
_ORIG_SUBPROCESS_POPEN = subprocess.Popen
_ORIG_SUBPROCESS_RUN = subprocess.run
_REAL_SUBPROCESS_TEST_FILES = frozenset(
    {
        _REPO_ROOT / "tests" / "opportunities" / "test_store.py",
        _REPO_ROOT / "tests" / "opportunities" / "test_replay.py",
        _REPO_ROOT / "tests" / "opportunities" / "test_import_boundary.py",
        _REPO_ROOT / "tests" / "opportunities" / "test_cli_e2e.py",
        _REPO_ROOT / "tests" / "opportunities" / "test_report_cli.py",
        _REPO_ROOT / "tests" / "catalog" / "test_import_boundary.py",
        _REPO_ROOT / "tests" / "rwa" / "test_cli_e2e.py",
        _REPO_ROOT / "tests" / "settled_cycles" / "test_import_boundary.py",
    }
)


def _guarded_open_common(file: Any, mode: str, *args: Any, **kwargs: Any) -> None:
    if isinstance(file, (str, Path, bytes)):
        try:
            resolved = Path(os.fsdecode(file)).resolve()
        except Exception:
            resolved = None

        if resolved:
            # Check read operations
            is_write = any(f in str(mode) for f in ("w", "a", "x", "+"))
            if not is_write and _is_forbidden_read_path(resolved):
                raise PermissionError(
                    f"Access to secret/credential or production repository is forbidden: {file!r}"
                )

            # Check write operations
            if is_write and _is_forbidden_write_path(resolved):
                raise PermissionError(
                    f"Writing outside /tmp is strictly forbidden during tests: {file!r}"
                )


def guarded_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
    mode = args[0] if args else kwargs.get("mode", "r")
    _guarded_open_common(file, str(mode), *args, **kwargs)
    return _ORIG_IO_OPEN(file, *args, **kwargs)


def guarded_builtins_open(file: Any, *args: Any, **kwargs: Any) -> Any:
    mode = args[0] if args else kwargs.get("mode", "r")
    _guarded_open_common(file, str(mode), *args, **kwargs)
    return _ORIG_BUILTINS_OPEN(file, *args, **kwargs)


def guarded_filehandler_init(
    self: logging.FileHandler,
    filename: Any,
    mode: str = "a",
    encoding: str | None = None,
    delay: bool = False,
    errors: str | None = None,
) -> None:
    try:
        resolved = Path(os.fsdecode(filename)).resolve()
        if _is_forbidden_write_path(resolved):
            filename = str(_COLLECTION_LOGS_DIR / resolved.name)
    except Exception:
        pass
    _ORIG_FILEHANDLER_INIT(self, filename, mode=mode, encoding=encoding, delay=delay, errors=errors)


def guarded_connect(self: socket.socket, address: Any) -> None:
    if getattr(self, "family", None) == socket.AF_UNIX:
        if not address:
            _ORIG_SOCK_CONNECT(self, address)
            return
        try:
            resolved = Path(os.fsdecode(address)).resolve()
            if resolved.is_relative_to(_TMP_ROOT):
                _ORIG_SOCK_CONNECT(self, address)
                return
        except Exception:
            pass
        raise RuntimeError(
            f"Connecting to host UNIX domain socket outside /tmp is forbidden: {address!r}"
        )
    raise RuntimeError(f"Network access forbidden in isolated test environment: {address}")


def guarded_socket_init(
    self: socket.socket,
    family: int = -1,
    type: int = -1,
    proto: int = -1,
    fileno: int | None = None,
) -> None:
    if family == socket.AF_UNIX:
        _ORIG_SOCK_INIT(self, family, type, proto, fileno)
        return
    raise RuntimeError(
        "Network access forbidden in isolated test environment: socket instantiation"
    )


def guarded_popen(*args: Any, **kwargs: Any) -> Any:
    caller = inspect.currentframe()
    while caller is not None:
        if Path(caller.f_code.co_filename) in _REAL_SUBPROCESS_TEST_FILES:
            return _ORIG_SUBPROCESS_POPEN(*args, **kwargs)
        caller = caller.f_back
    raise RuntimeError(f"Real subprocess creation forbidden in isolated tests: args={args}")


def guarded_run(*args: Any, **kwargs: Any) -> Any:
    caller = inspect.currentframe()
    while caller is not None:
        if Path(caller.f_code.co_filename) in _REAL_SUBPROCESS_TEST_FILES:
            return _ORIG_SUBPROCESS_RUN(*args, **kwargs)
        caller = caller.f_back
    raise RuntimeError(f"Real subprocess execution forbidden in isolated tests: args={args}")


# Install collection-time file open, logging, socket, and subprocess guards
io.open = guarded_io_open
builtins.open = guarded_builtins_open
setattr(logging.FileHandler, "__init__", guarded_filehandler_init)  # noqa: B010
setattr(socket.socket, "connect", guarded_connect)  # noqa: B010
setattr(socket.socket, "__init__", guarded_socket_init)  # noqa: B010
setattr(subprocess, "Popen", guarded_popen)  # noqa: B010
setattr(subprocess, "run", guarded_run)  # noqa: B010


# ==============================================================================
# RUNTIME FIXTURE FOR TEST EXECUTION ISOLATION
# ==============================================================================


@pytest.fixture(autouse=True)
def safety_test_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Per-test isolation harness: temp dirs, isolated cwd, network guard, and subprocess guard."""
    isolated_data = tmp_path / "data"
    isolated_logs = tmp_path / "logs"
    isolated_data.mkdir(parents=True, exist_ok=True)
    isolated_logs.mkdir(parents=True, exist_ok=True)

    # Synthetic JSONL ledger for ingestion tests (explicitly isolated per test in tmp_path)
    (isolated_data / "arbitrage_opportunities.jsonl").write_text(
        _SYNTHETIC_JSONL_OPPORTUNITY + "\n", encoding="utf-8"
    )

    # Confine working directory to tmp_path
    monkeypatch.chdir(tmp_path)

    # Set up test paths in environment
    monkeypatch.setenv("DEX_ENGINE_ROOT", str(tmp_path))
    monkeypatch.setenv("DEX_ENGINE_DATA_DIR", str(isolated_data))
    monkeypatch.setenv("DEX_ENGINE_LOGS_DIR", str(isolated_logs))
    monkeypatch.setenv("DEX_ENGINE_PID_FILE", str(isolated_logs / "arbitrage_daemon.pid"))
    monkeypatch.setenv("DEX_ENGINE_LOG_FILE", str(isolated_logs / "arbitrage_daemon.log"))
    monkeypatch.setenv(
        "DEX_ENGINE_STATUS_FILE", str(isolated_data / "arbitrage_daemon_status.json")
    )
    monkeypatch.setenv(
        "DEX_ENGINE_OPPORTUNITIES_FILE", str(isolated_data / "arbitrage_opportunities.jsonl")
    )
    monkeypatch.setenv("DEX_ENGINE_EVENTS_FILE", str(isolated_data / "arbitrage_events.jsonl"))

    # Update daemon module paths
    try:
        from monitors.daemons import arbitrage_daemon

        config_fn = getattr(arbitrage_daemon, "configure_daemon_paths", None)
        if callable(config_fn):
            config_fn(
                project_root=tmp_path,
                data_dir=isolated_data,
                logs_dir=isolated_logs,
            )
    except ImportError:
        pass

    yield
