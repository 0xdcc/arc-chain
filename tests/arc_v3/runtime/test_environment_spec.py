"""Tests for Arc Environment and Operations Specification (T41)

Verifies:
- systemd service template adherence to safety invariants (StartLimitBurst=3, DRY_RUN=True)
- Environment and Start/Stop runbook safety guarantees (no pkill wildcard, zero secret leakage)
- Clean sys.path boundaries
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestEnvironmentSpec:
    """Test suite for T41 operational specifications."""

    def test_service_template_invariants(self) -> None:
        service_file = _REPO_ROOT / "ops" / "arc-readonly.service.example"
        assert service_file.is_file()

        content = service_file.read_text(encoding="utf-8")
        assert "StartLimitBurst=3" in content
        assert 'Environment="DRY_RUN=True"' in content
        assert "Restart=on-failure" in content
        assert "--fixture-mode" in content

        # Verify no secret keywords in environment settings
        for line in content.splitlines():
            if line.startswith("Environment="):
                for forbidden in ("KEY=", "SECRET=", "TOKEN=", "PASSWORD=", "PRIVATE"):
                    assert forbidden not in line.upper()

    def test_environment_doc_contains_required_sections(self) -> None:
        env_doc = _REPO_ROOT / "ops" / "ENVIRONMENT.md"
        assert env_doc.is_file()

        content = env_doc.read_text(encoding="utf-8")
        assert "Environment Isolation Mandates" in content
        assert "runtime-data" in content
        assert "Dedicated Virtualenv" in content
        assert "Zero imports from" in content

    def test_start_stop_doc_prohibits_pkill(self) -> None:
        start_stop_doc = _REPO_ROOT / "ops" / "START_STOP.md"
        assert start_stop_doc.is_file()

        content = start_stop_doc.read_text(encoding="utf-8")
        assert "Never use wildcard" in content
        assert "pkill" in content
        assert "SIGTERM" in content

    def test_no_external_monolith_in_sys_path(self) -> None:
        for p in sys.path:
            p_str = str(Path(p).resolve())
            assert "dex-sniper-engine/core" not in p_str
            assert "dex-sniper-engine/chains" not in p_str
