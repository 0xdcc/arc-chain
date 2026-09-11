"""Partition every test obligation across isolated unit, host OS and read-only live layers."""

from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

LIVE = (
    "tests/test_robinhood.py::test_robinhood_rpc_connectivity_and_chain_id",
    "tests/test_robinhood.py::test_robinhood_web3_status",
    "tests/test_tick_cache.py::test_live_robinhood_decode_pons_pool",
)
HOST_OS = ("tests/test_safety_sandbox.py::test_real_launcher_controls",)
SUMMARY_PREFIX = "LAYER_RESULT "


def load_local(source: Path, name: str) -> Any:
    """Load an orchestration helper without site startup or editable installations."""
    specification = importlib.util.spec_from_file_location(name, source / "scripts" / f"{name}.py")
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Missing helper: {name}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def build_plan(source: Path) -> dict[str, Any]:
    """Assign all manifest test files; split only the three files spanning layers."""
    stage = load_local(source, "test_safety_stage")
    files = [
        name
        for name in stage.source_manifest(source)
        if name.startswith("tests/")
        and PurePosixPath(name).name.startswith("test_")
        and name.endswith(".py")
    ]
    special = set(LIVE + HOST_OS)
    unit: list[str] = []
    discovered: set[str] = set()
    fingerprints = {}
    for name in files:
        content = stage.read_source(source, name)[0]
        fingerprints[name] = hashlib.sha256(content).hexdigest()
        if not any(node.startswith(name + "::") for node in special):
            unit.append(name)
            continue
        for node in ast.parse(content, filename=name).body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ) or (isinstance(node, ast.ClassDef) and node.name.startswith("Test")):
                selector = f"{name}::{node.name}"
                discovered.add(selector)
                if selector not in special:
                    unit.append(selector)
    if not special <= discovered:
        raise RuntimeError(f"Unresolved layer obligations: {special - discovered}")
    return {"unit": unit, "host-os": list(HOST_OS), "live": list(LIVE), "test_sha256": fingerprints}


def clean_environment(runtime: Path) -> dict[str, str]:
    """Use a runtime-only environment for every child process."""
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(runtime),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "DEX_ENGINE_SAFE_CONFIG": "1",
    }


class Results:
    """Collect actual pytest outcomes without removing or rewriting any items."""

    def __init__(self, layer: str, live_policy: Any = None) -> None:
        self.layer = layer
        self.live_policy = live_policy
        self.collected: list[str] = []
        self.passed: set[str] = set()
        self.failed: set[str] = set()
        self.skipped: set[str] = set()
        self.deselected = 0
        self.collection_errors = 0

    def pytest_sessionstart(self, session: Any) -> None:
        """Enable the restricted real live transport after collection guards load."""
        if self.live_policy is not None:
            self.live_policy.activate_transport()

    def pytest_collection_finish(self, session: Any) -> None:
        """Verify runtime collection obeys the explicit layer assignment."""
        self.collected = [item.nodeid for item in session.items]
        for nodeid in self.collected:
            base = nodeid.split("[", 1)[0]
            expected = "live" if base in LIVE else "host-os" if base in HOST_OS else "unit"
            if expected != self.layer:
                raise RuntimeError(f"Wrong layer for {nodeid}: {self.layer}")
        if self.layer == "live" and set(self.collected) != set(LIVE):
            raise RuntimeError("Live collection must contain exactly the three original nodes")
        if self.layer == "host-os" and set(self.collected) != set(HOST_OS):
            raise RuntimeError("Host OS obligation missing")

    def pytest_collectreport(self, report: Any) -> None:
        """Keep collection failures separate from failed test calls."""
        if report.failed:
            self.collection_errors += 1

    def pytest_deselected(self, items: Any) -> None:
        """Reject deselection instead of reporting a partial suite as green."""
        self.deselected += len(items)

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Record call/setup/teardown results, including early-stop unexecuted nodes."""
        if report.failed:
            self.failed.add(report.nodeid)
            self.passed.discard(report.nodeid)
        elif report.skipped:
            self.skipped.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)
            if report.nodeid == LIVE[2]:
                print("PONS_DECODE_ORIGINAL_ASSERTIONS_PASSED", flush=True)

    def summary(self, code: int) -> dict[str, Any]:
        """Return exact collected/executed counts and any obligations left unrun."""
        unrun = set(self.collected) - self.passed - self.failed - self.skipped
        return {
            "layer": self.layer,
            "exit_code": code,
            "collected": len(self.collected),
            "passed": len(self.passed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "deselected": self.deselected,
            "not_run": len(unrun),
            "collection_errors": self.collection_errors,
            "nodes": self.collected,
            "failed_nodes": sorted(self.failed),
            "not_run_nodes": sorted(unrun),
        }


def quality_checks(source: Path) -> bool:
    """Run lint/format and the existing strict mypy gate inside the unit sandbox."""
    from scripts.test_safety_mypy_checker import check_mypy_output

    success = True
    for command in (
        [sys.executable, "-m", "ruff", "check", "--no-cache", "."],
        [sys.executable, "-m", "ruff", "format", "--no-cache", "--check", "."],
    ):
        result = subprocess.run(command, cwd=source, capture_output=True, text=True, check=False)
        print(shlex.join(command), flush=True)
        print(result.stdout + result.stderr, end="", flush=True)
        success &= result.returncode == 0
    mypy = subprocess.run(
        [sys.executable, "-m", "mypy", "."],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    print(mypy.stdout + mypy.stderr, end="", flush=True)
    success &= (
        check_mypy_output(
            mypy.returncode,
            mypy.stdout,
            mypy.stderr,
            source / "scripts/test_safety_mypy_baseline.txt",
        )
        == 0
    )
    print(
        f"QUALITY_RESULT {json.dumps({'passed': success, 'mypy_process_exit': mypy.returncode})}",
        flush=True,
    )
    return success


def worker(layer: str, source: Path, output: Path) -> int:
    """Run only assigned original pytest nodes in the appropriate execution environment."""
    if layer == "unit" and source != Path("/sandbox/src"):
        raise RuntimeError("Unit worker requires the OS sandbox source alias")
    sys.dont_write_bytecode = True
    dependencies = Path(sys.executable).parent.parent / "lib/python3.12/site-packages"
    sys.path.insert(0, str(dependencies))
    sys.path.insert(0, str(source))
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / f"runtime-{layer}"
    runtime.mkdir(exist_ok=True)
    tempfile.tempdir = str(runtime)
    os.chdir(source)
    plan = build_plan(source)
    quality_ok = quality_checks(source) if layer == "unit" else True
    policy = None
    if layer == "live":
        from scripts.test_safety_live import LivePolicy

        tree = ast.parse((source / "backtest/data/rpc_client.py").read_text())
        rpc_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RobinhoodRpc"
        )
        endpoint_assignment = next(
            node
            for node in rpc_class.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DEFAULT_URLS"
        )
        if endpoint_assignment.value is None:
            raise RuntimeError("Read-only RPC endpoint definition has no value")
        endpoints = set(ast.literal_eval(endpoint_assignment.value))
        policy = LivePolicy(source, output, endpoints)
        sys.addaudithook(policy.audit)
    import pytest

    plugin = Results(layer, policy)
    arguments = [
        *plan[layer],
        "-vv",
        "-s",
        "--tb=short",
        "-o",
        f"cache_dir={runtime / 'cache'}",
        f"--basetemp={runtime / 'pytest'}",
    ]
    if layer == "live":
        arguments.append("-x")
    code = int(pytest.main(arguments, plugins=[plugin]))
    result = plugin.summary(code)
    if not quality_ok or result["skipped"] or result["deselected"] or result["not_run"]:
        result["exit_code"] = 1
    print(SUMMARY_PREFIX + json.dumps(result), flush=True)
    return int(result["exit_code"])


def run_step(name: str, command: list[str], output: Path, cwd: Path) -> dict[str, Any]:
    """Persist raw stdout/stderr and command/exit status for one real process."""
    print(f"RUN {name}: {shlex.join(command)}", flush=True)
    started = datetime.datetime.now(datetime.UTC).isoformat()
    process = subprocess.run(
        command, cwd=cwd, env=clean_environment(output), capture_output=True, text=True, check=False
    )
    (output / f"{name}.stdout.log").write_text(process.stdout, encoding="utf-8")
    (output / f"{name}.stderr.log").write_text(process.stderr, encoding="utf-8")
    print(process.stdout + process.stderr, end="", flush=True)
    summaries = [
        json.loads(line[len(SUMMARY_PREFIX) :])
        for line in process.stdout.splitlines()
        if line.startswith(SUMMARY_PREFIX)
    ]
    return {
        "step": name,
        "command": command,
        "started_utc": started,
        "exit_code": process.returncode,
        "summary": summaries[-1] if summaries else None,
    }


def orchestrate(source: Path, layer: str) -> int:
    """Execute all requested layers independently; any failed or missing layer blocks acceptance."""
    output = Path(tempfile.mkdtemp(prefix="dex-layers-", dir="/tmp"))
    staged = output / "source"
    staged.mkdir()
    load_local(source, "test_safety_stage").stage_sources(source, staged)
    dependency = source / "venv"
    if not (dependency / "bin/python").is_file():
        raise RuntimeError("Missing local virtual environment")
    (staged / "venv").symlink_to(dependency.resolve())
    python = str(dependency / "bin/python")
    plan = build_plan(staged)
    (output / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    steps = []
    print(f"EVIDENCE_DIR {output}", flush=True)
    for current in ("unit", "host-os", "live"):
        if layer not in ("all", current):
            continue
        if current == "host-os":
            steps.append(
                run_step(
                    "host-os-probe",
                    [
                        python,
                        "-I",
                        "-S",
                        str(staged / "scripts/test_safety_sandbox_probe.py"),
                        "--verify-launcher",
                    ],
                    output,
                    staged,
                )
            )
        if current == "unit":
            command = [
                "/bin/bash",
                str(source / "scripts/test_safety_sandbox.sh"),
                "/sandbox/venv/bin/python",
                "-I",
                "-S",
                "/sandbox/src/scripts/test_safety_layers.py",
                "--worker",
                current,
                "--source",
                "/sandbox/src",
                "--output",
                "/tmp/layer-unit",
            ]
        else:
            command = [
                python,
                "-I",
                "-S",
                str(staged / "scripts/test_safety_layers.py"),
                "--worker",
                current,
                "--source",
                str(staged),
                "--output",
                str(output),
            ]
        steps.append(run_step(current, command, output, staged))
        (output / "results.json").write_text(json.dumps(steps, indent=2), encoding="utf-8")
    success = all(step["exit_code"] == 0 for step in steps)
    success &= all(step["summary"] is not None for step in steps if step["step"] != "host-os-probe")
    print(
        f"ALL_LAYERS_RESULT {json.dumps({'requested': layer, 'passed': success, 'evidence': str(output)})}",
        flush=True,
    )
    return 0 if success else 1


def main() -> int:
    """Run the full authorized gate, or a clearly labeled individual verification layer."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=("all", "unit", "host-os", "live"), default="all")
    parser.add_argument("--worker", choices=("unit", "host-os", "live"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if args.plan:
        print(json.dumps(build_plan(args.source), indent=2))
        return 0
    if args.worker:
        if args.output is None:
            parser.error("Worker requires --output")
        return worker(args.worker, args.source, args.output)
    return orchestrate(args.source, args.layer)


if __name__ == "__main__":
    sys.exit(main())
