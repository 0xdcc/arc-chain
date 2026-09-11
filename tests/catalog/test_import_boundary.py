"""C22 clean child closure, independent side-effect probes and fixture inventory gate."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/catalog/v1"
CHILD = r"""
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "venv/lib/python3.12/site-packages"))
sys.path.insert(0, str(root))
forbidden = {"chains", "core", "arbitrage", "socket", "requests", "monitors", "execution", "subprocess"}
assert not forbidden.intersection(name.split(".")[0] for name in sys.modules)
blocked = []
process_attempts = []
def guard(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.fork"):
        blocked.append(event)
        if event == "subprocess.Popen":
            process_attempts.append(args[1])
        raise PermissionError("C22 side effect blocked")
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = str(args[0])
        if ".env" in name or "keystore" in name or "mock_secret" in name:
            blocked.append(event)
            raise PermissionError("C22 secret read blocked")
sys.addaudithook(guard)
import market_catalog
from market_catalog import CatalogRegistry, load_inputs
from market_catalog.discovery import discover_pools
from market_catalog.verification import compute_v4_pool_id, ZERO_ADDRESS, WETH_ADDRESS
catalog = CatalogRegistry(load_inputs(root / "tests/fixtures/catalog/v1/synthetic_valid.jsonl"), domain="synthetic")
assert len(catalog.list_assets()) == 4
assert not forbidden.intersection(name.split(".")[0] for name in sys.modules)
assert not blocked, blocked
assert Path(market_catalog.__file__).resolve() == root / "market_catalog/__init__.py"
assert not any(name in market_catalog.__dict__ for name in ("broadcast", "wallet", "approve", "notify"))
print("PURE_IMPORT_AND_DISCOVERY_PASS")
"""
PROBES = r"""
# These are boundary self-tests after the pure module closure has been asserted.
import socket
for action in (
    lambda: socket.socket(),
    lambda: open("/tmp/mock_secret_w1b.env"),
    lambda: __import__("subprocess").run([sys.executable, "-c", "raise SystemExit(99)"]),
):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError("Boundary probe escaped")
assert len(blocked) == 3, blocked
print("NETWORK_SECRET_SUBPROCESS_BLOCKED")
"""


CRYPTO = r"""
# Dependency metadata imports email.utils -> socket; the Keccak backend imports
# ctypes.util -> subprocess. The installed ctypes backend tries platform.architecture
# via file -b; the audit guard denies it and the dependency uses its local fallback.
from eth_abi import encode
from eth_abi.abi import encode as definition_encode
from eth_utils import keccak
from eth_utils.crypto import keccak as definition_keccak
assert encode is definition_encode and keccak is definition_keccak
assert compute_v4_pool_id(ZERO_ADDRESS, WETH_ADDRESS, 3000, 60, ZERO_ADDRESS) == "0x5fb36fcf81a62ac30e44924e98284b317a1fbfdd7b223b788319c663baebc930"
legacy = {"chains", "core", "arbitrage", "requests", "monitors", "execution"}
assert not legacy.intersection(name.split(".")[0] for name in sys.modules)
for name in ("socket", "subprocess"):
    assert Path(sys.modules[name].__file__).resolve().is_relative_to(sys.base_prefix)
assert blocked == ["subprocess.Popen"], blocked
assert len(process_attempts) == 1 and process_attempts[0][:2] == ["file", "-b"]
assert not any(event.startswith("socket.") for event in blocked)
print("ABI_KECCAK_NO_NETWORK_OR_LEGACY_PASS")
"""


def child(code: str) -> subprocess.CompletedProcess[str]:
    """Run with no inherited credentials or Python startup customization."""
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(ROOT)],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        cwd="/tmp",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_pure_import_boundary_zero_leak() -> None:
    result = child(CHILD)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "PURE_IMPORT_AND_DISCOVERY_PASS"


def test_network_secret_subprocess_blocked_independently() -> None:
    result = child(CHILD + PROBES)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NETWORK_SECRET_SUBPROCESS_BLOCKED" in result.stdout


def test_crypto_dependency_boundary_no_network_or_legacy() -> None:
    result = child(CHILD + CRYPTO)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ABI_KECCAK_NO_NETWORK_OR_LEGACY_PASS" in result.stdout


def test_static_import_boundary() -> None:
    allowed = {
        "__future__",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "pathlib",
        "typing",
        "io",
        "arbitrage_contracts",
        "eth_abi",
        "eth_utils",
    }
    for path in (ROOT / "market_catalog").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in allowed for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module is not None
                assert node.module.split(".")[0] in allowed
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "__import__"}


@pytest.mark.parametrize("option", ["--help", "--broadcast", "--wallet", "--approve", "--notify"])
def test_no_cli_execution_or_approval_entry(option: str) -> None:
    # W1-B supplies no CLI. Python must reject all requested CLI entry invocations.
    result = child(
        CHILD
        + f'\nimport runpy\nsys.argv = ["market_catalog", {option!r}]\n'
        + 'runpy.run_module("market_catalog", run_name="__main__")\n'
    )
    assert result.returncode != 0
    assert "No module named market_catalog.__main__" in result.stderr


def assert_inventory(root: Path) -> None:
    """Compare both directions; neither new code nor ghost entries may escape review."""
    fixture_root = root / "tests/fixtures/catalog/v1"
    manifest = json.loads((fixture_root / "manifest.json").read_text())
    registered = manifest["catalog_files"]
    disk = {
        str(path.relative_to(root))
        for folder in ("market_catalog", "tests/catalog")
        for path in (root / folder).rglob("*.py")
    }
    assert len(registered) == len(set(registered)), "Duplicate catalog inventory entry"
    assert disk == set(registered), f"Catalog inventory mismatch: {disk ^ set(registered)}"
    partitions = manifest["partitions"]
    assert {path.name for path in fixture_root.glob("*.jsonl")} == set(partitions)
    for name, digest in partitions.items():
        assert hashlib.sha256((fixture_root / name).read_bytes()).hexdigest() == digest
    review_text = manifest["review_manifest_text"]
    assert (
        hashlib.sha256(review_text.encode()).hexdigest()
        == manifest["review_trust"]["manifest_sha256"]
    )


def test_manifest_inventory_exact() -> None:
    assert_inventory(ROOT)
