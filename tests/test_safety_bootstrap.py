"""Unit tests for Safety Bootstrap (Batch 1A).

Tests global isolation harnesses, constructor defaults, CLI parameter roundtrip,
cmd_stop liveness verification, production main() entry point, and fixture separation.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from core.config import Config, load_safe_config
from execution.weth_arbitrage_executor import (
    CANONICAL_USDG_ADDRESS,
    CANONICAL_WETH_ADDRESS,
)
from monitors.daemons import arbitrage_daemon
from monitors.daemons.arbitrage_daemon import (
    ArbitrageDaemon,
    build_daemon_cmd,
    build_parser,
    cmd_stop,
    configure_daemon_paths,
    main,
    notify_chain_auditor,
)
from web3 import Web3


def _build_test_cli_parser() -> argparse.ArgumentParser:
    """Return the real production CLI parser."""
    return build_parser()


def test_daemon_constructor_defaults_to_monitor_only() -> None:
    """Daemon constructor must default to auto_execute=False (monitor-only)."""
    daemon = ArbitrageDaemon()
    assert daemon.auto_execute is False


def test_cli_parser_defaults_to_monitor_only() -> None:
    """Daemon CLI parser must default to auto_execute=False unless explicitly requested."""
    parser = build_parser()

    # When --auto-execute is omitted, it must be False
    args_default = parser.parse_args([])
    assert args_default.auto_execute is False

    # When --auto-execute is passed, it must be True
    args_enabled = parser.parse_args(["--auto-execute"])
    assert args_enabled.auto_execute is True

    # When --no-auto-execute is explicitly passed, it must be False
    args_disabled = parser.parse_args(["--no-auto-execute"])
    assert args_disabled.auto_execute is False


def test_cmd_start_argv_roundtrip_preserves_monitor_only_and_safety_params() -> None:
    """Parent cmd_start with auto_execute=False must roundtrip to child with auto_execute=False."""
    parser = build_parser()

    parent_args = argparse.Namespace(
        mode="spread",
        min_tvl=50000.0,
        interval=3.0,
        max_burst=5,
        auto_execute=False,
        min_profit=0.3,
        buffer=0.35,
        triangle_buffer=0.75,
        base_token="USDG",
        one_shot_probe=True,
        refresh_pools=True,
        allow_v4=False,
        enable_feed=False,
        feed_url=None,
        ws_rpc_url=None,
    )

    child_cmd = build_daemon_cmd(parent_args)

    # Must contain explicit --no-auto-execute
    assert "--no-auto-execute" in child_cmd
    assert "--auto-execute" not in child_cmd

    # Must pass safety params
    assert "--base-token" in child_cmd
    base_idx = child_cmd.index("--base-token")
    assert child_cmd[base_idx + 1] == "USDG"

    assert "--buffer" in child_cmd
    buffer_idx = child_cmd.index("--buffer")
    assert float(child_cmd[buffer_idx + 1]) == 0.35

    assert "--triangle-buffer" in child_cmd
    tri_idx = child_cmd.index("--triangle-buffer")
    assert float(child_cmd[tri_idx + 1]) == 0.75

    # Child CLI parser roundtrip verification (strip binary and python flags)
    # Child cmd starts with: [python_bin, -m, monitors.daemons.arbitrage_daemon, ...]
    parsed_child_args = parser.parse_args(child_cmd[3:])
    assert parsed_child_args.auto_execute is False
    assert parsed_child_args.base_token == "USDG"
    assert parsed_child_args.buffer == 0.35
    assert parsed_child_args.triangle_buffer == 0.75
    assert parsed_child_args.mode == "spread"
    assert parsed_child_args.min_tvl == 50000.0
    assert parsed_child_args.interval == 3.0
    assert parsed_child_args.max_burst == 5
    assert parsed_child_args.one_shot_probe is True
    assert parsed_child_args.refresh_pools is True


def test_cmd_start_argv_roundtrip_preserves_explicit_auto_execute() -> None:
    """Parent cmd_start with auto_execute=True passes --auto-execute and --min-profit to child."""
    parser = build_parser()

    parent_args = argparse.Namespace(
        mode="all",
        min_tvl=100000.0,
        interval=2.0,
        max_burst=10,
        auto_execute=True,
        min_profit=0.25,
        buffer=0.2,
        triangle_buffer=0.6,
        base_token=None,
        one_shot_probe=False,
        refresh_pools=False,
        allow_v4=True,
        enable_feed=True,
        feed_url="wss://custom-feed",
        ws_rpc_url="wss://custom-rpc",
    )

    child_cmd = build_daemon_cmd(parent_args)

    assert "--auto-execute" in child_cmd
    assert "--no-auto-execute" not in child_cmd

    assert "--min-profit" in child_cmd
    profit_idx = child_cmd.index("--min-profit")
    assert float(child_cmd[profit_idx + 1]) == 0.25

    parsed_child_args = parser.parse_args(child_cmd[3:])
    assert parsed_child_args.auto_execute is True
    assert parsed_child_args.min_profit == 0.25
    assert parsed_child_args.allow_v4 is True
    assert parsed_child_args.enable_feed is True
    assert parsed_child_args.feed_url == "wss://custom-feed"
    assert parsed_child_args.ws_rpc_url == "wss://custom-rpc"


def test_production_main_start_dispatches_with_monitor_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production main() with --start should parse arguments and pass them to cmd_start."""
    mock_cmd_start = MagicMock()
    monkeypatch.setattr("monitors.daemons.arbitrage_daemon.cmd_start", mock_cmd_start)

    main(["--start", "--mode", "triangle", "--base-token", "USDG"])
    mock_cmd_start.assert_called_once()
    args = mock_cmd_start.call_args[0][0]
    assert args.start is True
    assert args.auto_execute is False
    assert args.mode == "triangle"
    assert args.base_token == "USDG"


def test_production_main_start_explicit_auto_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production main() with --start --auto-execute correctly propagates True."""
    mock_cmd_start = MagicMock()
    monkeypatch.setattr("monitors.daemons.arbitrage_daemon.cmd_start", mock_cmd_start)

    main(["--start", "--auto-execute", "--min-profit", "0.5"])
    mock_cmd_start.assert_called_once()
    args = mock_cmd_start.call_args[0][0]
    assert args.auto_execute is True
    assert args.min_profit == 0.5


def test_production_main_stop_dispatches_cmd_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production main() with --stop dispatches to cmd_stop()."""
    mock_cmd_stop = MagicMock(return_value=True)
    monkeypatch.setattr("monitors.daemons.arbitrage_daemon.cmd_stop", mock_cmd_stop)

    main(["--stop"])
    mock_cmd_stop.assert_called_once()


def test_production_main_status_dispatches_cmd_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production main() with --status dispatches to cmd_status()."""
    mock_cmd_status = MagicMock()
    monkeypatch.setattr("monitors.daemons.arbitrage_daemon.cmd_status", mock_cmd_status)

    main(["--status"])
    mock_cmd_status.assert_called_once()


def test_production_main_foreground_dispatches_arbitrage_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production main() with --foreground creates daemon with parsed args and runs."""
    mock_daemon_cls = MagicMock()
    mock_daemon_inst = MagicMock()
    mock_daemon_cls.return_value = mock_daemon_inst
    monkeypatch.setattr("monitors.daemons.arbitrage_daemon.ArbitrageDaemon", mock_daemon_cls)

    main(["--foreground", "--mode", "spread", "--base-token", "USDG"])
    mock_daemon_cls.assert_called_once()
    kwargs = mock_daemon_cls.call_args[1]
    assert kwargs["mode"] == "spread"
    assert kwargs["base_token"] == "USDG"
    assert kwargs["auto_execute"] is False
    mock_daemon_inst.run_forever.assert_called_once()


def test_production_main_no_action_shows_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Production main() without action arguments prints help without error."""
    main([])
    captured = capsys.readouterr()
    assert "usage:" in captured.out or "usage:" in captured.err


def test_cmd_stop_still_alive_does_not_delete_pid_and_returns_false(tmp_path: Path) -> None:
    """When target process survives SIGTERM and remains alive, cmd_stop must NOT delete PID."""
    test_pid_file = tmp_path / "test_daemon.pid"
    test_pid = 999999
    test_pid_file.write_text(str(test_pid), encoding="utf-8")

    configure_daemon_paths(pid_file=test_pid_file)

    kill_calls: list[tuple[int, int]] = []

    def stub_killer(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        return

    result = cmd_stop(timeout=0.2, poll_interval=0.01, kill_fn=stub_killer)

    # Must return False (failed stop)
    assert result is False

    # PID file must STILL EXIST for post-mortem evidence
    assert test_pid_file.exists()
    assert test_pid_file.read_text(encoding="utf-8") == str(test_pid)

    # Must have sent SIGTERM
    assert (test_pid, signal.SIGTERM) in kill_calls


def test_cmd_stop_process_exits_deletes_pid_and_reports_success(tmp_path: Path) -> None:
    """When process terminates on SIGTERM, PID file must be cleanly deleted."""
    test_pid_file = tmp_path / "test_daemon.pid"
    test_pid = 888888
    test_pid_file.write_text(str(test_pid), encoding="utf-8")

    configure_daemon_paths(pid_file=test_pid_file)

    state = {"terminated": False}

    def stub_killer(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            state["terminated"] = True
            return
        if sig == 0:
            if state["terminated"]:
                raise ProcessLookupError("Process does not exist")
            return

    result = cmd_stop(timeout=0.2, poll_interval=0.01, kill_fn=stub_killer)

    assert result is True
    assert not test_pid_file.exists()


def test_cmd_stop_no_running_pid(tmp_path: Path) -> None:
    """cmd_stop when no PID file exists returns True cleanly without attempting kills."""
    test_pid_file = tmp_path / "non_existent.pid"
    configure_daemon_paths(pid_file=test_pid_file)

    mock_killer = MagicMock()
    result = cmd_stop(kill_fn=mock_killer)
    assert result is True
    mock_killer.assert_not_called()


def test_global_isolation_blocks_unmocked_network() -> None:
    """Unmocked network connections must raise RuntimeError in tests."""
    with pytest.raises(RuntimeError) as exc_info:
        sock = socket.socket()
        sock.connect(("1.1.1.1", 80))
    assert "Network access forbidden" in str(exc_info.value)


def test_global_isolation_blocks_unmocked_subprocess() -> None:
    """Unmocked subprocess calls must raise RuntimeError in tests."""
    with pytest.raises(RuntimeError) as exc_info_popen:
        subprocess.Popen(["echo", "danger"])
    assert "Real subprocess creation forbidden" in str(exc_info_popen.value)

    with pytest.raises(RuntimeError) as exc_info_run:
        subprocess.run(["echo", "danger"])
    assert "Real subprocess execution forbidden" in str(exc_info_run.value)


def test_global_isolation_blocks_production_file_writes() -> None:
    """Writes to production engine paths or repo data/logs must raise PermissionError."""
    prod_path = Path("/root/projects/crypto/dex-sniper-engine/data/arbitrage_events.jsonl")
    with pytest.raises(PermissionError):
        with open(prod_path, "w", encoding="utf-8") as f:
            f.write("polluted\n")

    repo_root = Path(__file__).resolve().parent.parent
    with pytest.raises(PermissionError):
        (repo_root / "logs" / "test_forbidden.log").write_text("danger", encoding="utf-8")


def test_global_isolation_blocks_secret_path_reads() -> None:
    """Access to /root/.secrets or non-tmp .env/credential files must raise PermissionError."""
    with pytest.raises(PermissionError):
        open("/root/.secrets/id_rsa", encoding="utf-8")

    with pytest.raises(PermissionError):
        open(_REPO_ROOT / ".env", encoding="utf-8")

    with pytest.raises(PermissionError):
        open(_PROD_ROOT / ".env", encoding="utf-8")

    with pytest.raises(PermissionError):
        open("/tmp_blocked/.hermes/credentials", encoding="utf-8")


def test_notify_chain_auditor_writes_to_isolated_tmp_path(tmp_path: Path) -> None:
    """notify_chain_auditor must write events to test-isolated directory, not production."""
    test_event = {"event_type": "PROBE_SUCCESS", "profit": 1.0}
    notify_chain_auditor(test_event)

    isolated_events_file = arbitrage_daemon.EVENTS_FILE
    assert isolated_events_file.exists()
    assert str(tmp_path) in str(isolated_events_file)

    content = isolated_events_file.read_text(encoding="utf-8")
    data = json.loads(content.strip().splitlines()[-1])
    assert data["event_type"] == "PROBE_SUCCESS"


def test_local_mocks_override_global_isolation() -> None:
    """Individual tests must be able to use standard unittest mocks without global interference."""
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        res = subprocess.run(["arbitrary", "command"])
        assert res.returncode == 0
        mock_run.assert_called_once()


def test_usdg_and_weth_contract_mocks_are_distinct() -> None:
    """w3.eth.contract must return distinct mock instances for different contract addresses."""
    w3 = MagicMock()
    contracts: dict[str, MagicMock] = {}

    def _get_contract(address: str | None = None, **kwargs: Any) -> MagicMock:
        key = Web3.to_checksum_address(address) if address else "default"
        if key not in contracts:
            contracts[key] = MagicMock(name=f"Mock_{key[:8]}")
        return contracts[key]

    w3.eth.contract.side_effect = _get_contract

    weth_mock = w3.eth.contract(address=CANONICAL_WETH_ADDRESS)
    usdg_mock = w3.eth.contract(address=CANONICAL_USDG_ADDRESS)

    assert weth_mock is not usdg_mock

    # Mutating USDG mock does not mutate WETH mock
    usdg_mock.functions.balanceOf.return_value.call.return_value = 500 * 10**6
    weth_mock.functions.balanceOf.return_value.call.return_value = 10**18

    assert usdg_mock.functions.balanceOf().call() == 500 * 10**6
    assert weth_mock.functions.balanceOf().call() == 10**18


def test_load_safe_config_seam_without_env() -> None:
    """load_safe_config must instantiate Config safely without loading .env."""
    cfg = load_safe_config(_env_file=None, DRY_RUN=True, MAX_TRADE_AMOUNT_USD=100.0)
    assert cfg.DRY_RUN is True
    assert cfg.MAX_TRADE_AMOUNT_USD == 100.0


def test_config_dotenv_loading_and_safe_factory_control(tmp_path: Path) -> None:
    """Verify that Config() preserves standard .env loading while load_safe_config(_env_file=None) skips it."""
    # Write a test .env file into tmp_path
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_TRADE_AMOUNT_USD=123.0\n", encoding="utf-8")

    # Config() without arguments loads 123.0 from cwd .env (preserving Pydantic default dotenv semantics)
    cfg_default = Config()
    assert cfg_default.MAX_TRADE_AMOUNT_USD == 123.0

    # Config() with explicit _env_file loads 123.0
    cfg_loaded = Config(_env_file=env_file)
    assert cfg_loaded.MAX_TRADE_AMOUNT_USD == 123.0

    # Explicit safe factory load_safe_config(_env_file=None) ignores the .env file
    cfg_safe = load_safe_config(_env_file=None)
    assert cfg_safe.MAX_TRADE_AMOUNT_USD == 500.0


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROD_ROOT = Path("/root/projects/crypto/dex-sniper-engine")
_CHECKER_PATH = str(_REPO_ROOT / "scripts" / "test_safety_mypy_checker.py")


def _get_check_mypy_output() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("test_safety_mypy_checker_module", _CHECKER_PATH)
    assert spec and spec.loader
    checker_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker_mod)
    return checker_mod.check_mypy_output


def test_mypy_checker_fail_closed_on_crash(tmp_path: Path) -> None:
    """Mypy checker must fail-closed (return 1) on tool crash/exit code 2 even without error lines."""
    check_mypy_output = _get_check_mypy_output()
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")

    # Exit code 2 (mypy internal crash or fatal exception)
    code = check_mypy_output(
        returncode=2,
        stdout="",
        stderr="mypy: internal crash: fatal unhandled exception",
        baseline_path=baseline,
    )
    assert code == 1


def test_mypy_checker_fail_closed_on_unparseable_output(tmp_path: Path) -> None:
    """Mypy checker must fail-closed (return 1) on exit code 1 with unparseable output."""
    check_mypy_output = _get_check_mypy_output()
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="Some non-standard error output without colons",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 1


def test_mypy_checker_passes_on_exact_baseline(tmp_path: Path) -> None:
    """Mypy checker returns 0 when exit code is 1 and all errors match the baseline."""
    check_mypy_output = _get_check_mypy_output()

    baseline = tmp_path / "baseline.txt"
    baseline.write_text("module.py:10: error: Some known error  [code]\n", encoding="utf-8")

    code = check_mypy_output(
        returncode=1,
        stdout="module.py:10: error: Some known error  [code]\nFound 1 error in 1 file",
        stderr="",
        baseline_path=baseline,
    )
    assert code == 0


def test_path_isolation_rejects_tmp_prefix_spoofing() -> None:
    """Paths starting with /tmp-fake or /tmp_malicious must NOT bypass write isolation."""
    fake_tmp = Path("/tmp-malicious/danger.log")
    with pytest.raises(PermissionError):
        open(fake_tmp, "w", encoding="utf-8")


def test_nonexistent_jsonl_raises_file_not_found(tmp_path: Path) -> None:
    """Nonexistent JSONL files must raise FileNotFoundError, proving fake fallback was removed."""
    from core.wallet_guard import WalletGuard
    from execution.weth_arbitrage_executor import WethArbitrageExecutor

    w3 = MagicMock()
    cfg = load_safe_config(_env_file=None, DRY_RUN=True)
    guard = WalletGuard(dry_run=True)
    executor = WethArbitrageExecutor(config=cfg, guard=guard, w3=w3)

    non_existent = tmp_path / "does_not_exist.jsonl"
    with pytest.raises(FileNotFoundError):
        executor.load_opportunities_from_jsonl(non_existent)


def test_cache_directory_name_whitelist_is_removed() -> None:
    """REVIEW_1A_INDEPENDENT.md P1: Any write to .pytest_cache outside /tmp must be forbidden."""
    prod_cache_path = Path("/root/projects/crypto/dex-sniper-engine/.pytest_cache/probe")
    with pytest.raises(PermissionError):
        open(prod_cache_path, "w", encoding="utf-8")


def test_production_data_reading_is_forbidden() -> None:
    """REVIEW_1A_INDEPENDENT.md P4: Reading production data files must be strictly forbidden."""
    prod_data_path = Path("/root/projects/crypto/dex-sniper-engine/data/arbitrage_events.jsonl")
    with pytest.raises(PermissionError):
        open(prod_data_path, encoding="utf-8")
