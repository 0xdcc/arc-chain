"""Targeted regression tests for pytest collection import mode and module collision prevention."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tools.qa.upstream_obligations import get_repo_root

REPO_ROOT = get_repo_root()


class TestCollectionImportMode:
    """Verification suite ensuring pytest collection avoids module collisions without modifying testpaths."""

    def test_pyproject_toml_pytest_import_mode_configuration(self) -> None:
        pyproject_path = REPO_ROOT / "pyproject.toml"
        assert pyproject_path.is_file(), f"Missing pyproject.toml at {pyproject_path}"

        toml_payload = pyproject_path.read_text(encoding="utf-8")
        parsed_config = tomllib.loads(toml_payload)

        assert "tool" in parsed_config
        assert "pytest" in parsed_config["tool"]
        assert "ini_options" in parsed_config["tool"]["pytest"]

        ini_options = parsed_config["tool"]["pytest"]["ini_options"]
        assert "addopts" in ini_options

        addopts = ini_options["addopts"]
        if isinstance(addopts, list):
            assert "--import-mode=importlib" in addopts
        else:
            assert "--import-mode=importlib" in addopts.split()

        assert "testpaths" not in ini_options

    def test_duplicate_module_basenames_exist_across_subdirectories(self) -> None:
        collision_groups = [
            (
                REPO_ROOT / "tests" / "arc_v3" / "simulation" / "test_reconciliation.py",
                REPO_ROOT / "tests" / "atomic_execution" / "test_reconciliation.py",
            ),
            (
                REPO_ROOT / "tests" / "catalog" / "test_cli.py",
                REPO_ROOT / "tests" / "atomic_execution" / "test_cli.py",
                REPO_ROOT / "tests" / "settled_cycles" / "test_cli.py",
            ),
            (
                REPO_ROOT / "tests" / "catalog" / "test_inputs.py",
                REPO_ROOT / "tests" / "atomic_execution" / "test_inputs.py",
            ),
        ]

        for file_group in collision_groups:
            first_name = file_group[0].name
            for file_path in file_group:
                assert file_path.is_file(), f"Expected test file missing: {file_path}"
                assert file_path.name == first_name

    def test_pytest_collection_mode_behavior_differential(self) -> None:
        test_file_arc = str(REPO_ROOT / "tests" / "arc_v3" / "simulation" / "test_reconciliation.py")
        test_file_atomic = str(REPO_ROOT / "tests" / "atomic_execution" / "test_reconciliation.py")

        prepend_exit_code = pytest.main(
            [
                "--collect-only",
                "-q",
                "--import-mode=prepend",
                test_file_arc,
                test_file_atomic,
                "-o",
                "cache_dir=/tmp/collection_test_cache_prepend",
            ]
        )
        assert prepend_exit_code == pytest.ExitCode.INTERRUPTED

        importlib_exit_code = pytest.main(
            [
                "--collect-only",
                "-q",
                "--import-mode=importlib",
                test_file_arc,
                test_file_atomic,
                "-o",
                "cache_dir=/tmp/collection_test_cache_importlib",
            ]
        )
        assert importlib_exit_code == pytest.ExitCode.OK

        default_config_exit_code = pytest.main(
            [
                "--collect-only",
                "-q",
                test_file_arc,
                test_file_atomic,
                "-o",
                "cache_dir=/tmp/collection_test_cache_default",
            ]
        )
        assert default_config_exit_code == pytest.ExitCode.OK
