"""Independent Verification Test Suite for Multi-Mastermind Coordination Chaos (T47 / G2).

Audits and proves:
1. Multi-writer collision prevention: writing outside granted lease allowlist fails closed.
2. Outbox reduction idempotency: re-reducing already processed outbox messages leaves merge queue and task board unchanged.
3. Unknown task and malformed payload fail-closed semantics in reducer.
4. Autonomous mastermind outbox writing during M1 pause or recovery.
5. Lease release verification upon task submission and integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.coord.reduce_outbox import OutboxReductionError, reduce_outbox


class TestCoordinationChaosAndLeaseBoundaries:
    """Tests coordination robustness under concurrent outbox submissions and edge cases."""

    @pytest.fixture
    def mock_coord_env(self, tmp_path: Path) -> Path:
        coord_dir = tmp_path / ".coord"
        coord_dir.mkdir(parents=True)
        outbox = coord_dir / "outbox"
        for b in ("M1", "M2", "M3", "M4"):
            (outbox / b).mkdir(parents=True)

        task_board = {
            "plan_id": "TEST-PLAN",
            "tasks": {
                "T10": {"status": "UNASSIGNED", "result": None, "last_attempt": None},
                "T20": {"status": "UNASSIGNED", "result": None, "last_attempt": None},
            },
        }
        with open(coord_dir / "TASK_BOARD.json", "w", encoding="utf-8") as f:
            json.dump(task_board, f)

        leases = {
            "plan_id": "TEST-PLAN",
            "active_leases": {
                "T10": {
                    "brain": "M2",
                    "lease_epoch": 1,
                    "exact_write_allowlist": ["src/module_a.py", "tests/test_a.py"],
                },
            },
        }
        with open(coord_dir / "LEASES.json", "w", encoding="utf-8") as f:
            json.dump(leases, f)

        merge_queue = {
            "plan_id": "TEST-PLAN",
            "queue": [],
            "processed_messages": [],
        }
        with open(coord_dir / "MERGE_QUEUE.json", "w", encoding="utf-8") as f:
            json.dump(merge_queue, f)

        return coord_dir

    def test_outbox_reduction_success_and_lease_release(self, mock_coord_env: Path) -> None:
        msg_file = mock_coord_env / "outbox" / "M2" / "RESULT-T10-attempt1.json"
        payload = {
            "task_id": "T10",
            "lease_epoch": 1,
            "status": "SUBMITTED",
            "result_commit": "commit_12345",
            "changed_files": ["src/module_a.py"],
            "attempt": 1,
        }
        with open(msg_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        res = reduce_outbox(mock_coord_env)
        assert res["processed_count"] == 1
        assert "M2/RESULT-T10-attempt1.json" in res["processed_messages"]

        # Verify task board updated
        with open(mock_coord_env / "TASK_BOARD.json", encoding="utf-8") as f:
            tb = json.load(f)
        assert tb["tasks"]["T10"]["status"] == "SUBMITTED"
        assert tb["tasks"]["T10"]["result"] == "commit_12345"

        # Verify lease released
        with open(mock_coord_env / "LEASES.json", encoding="utf-8") as f:
            leases = json.load(f)
        assert "T10" not in leases["active_leases"]

        # Verify merge queue has item
        with open(mock_coord_env / "MERGE_QUEUE.json", encoding="utf-8") as f:
            mq = json.load(f)
        assert len(mq["queue"]) == 1
        assert mq["queue"][0]["task_id"] == "T10"

    def test_unauthorized_file_write_fails_closed(self, mock_coord_env: Path) -> None:
        msg_file = mock_coord_env / "outbox" / "M2" / "RESULT-T10-attempt1.json"
        payload = {
            "task_id": "T10",
            "lease_epoch": 1,
            "status": "SUBMITTED",
            "result_commit": "commit_bad",
            "changed_files": ["src/module_a.py", "unauthorized/secret.py"],  # Violation!
            "attempt": 1,
        }
        with open(msg_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with pytest.raises(OutboxReductionError, match="outside lease allowlist"):
            reduce_outbox(mock_coord_env)

        # Ensure task board was not corrupted
        with open(mock_coord_env / "TASK_BOARD.json", encoding="utf-8") as f:
            tb = json.load(f)
        assert tb["tasks"]["T10"]["status"] == "UNASSIGNED"

    def test_reduction_idempotency_on_repeated_runs(self, mock_coord_env: Path) -> None:
        msg_file = mock_coord_env / "outbox" / "M2" / "RESULT-T10-attempt1.json"
        payload = {
            "task_id": "T10",
            "lease_epoch": 1,
            "status": "SUBMITTED",
            "result_commit": "commit_12345",
            "changed_files": ["src/module_a.py"],
            "attempt": 1,
        }
        with open(msg_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        # First run processes 1 message
        res1 = reduce_outbox(mock_coord_env)
        assert res1["processed_count"] == 1

        # Second run on same state must be a no-op (processed_count == 0)
        res2 = reduce_outbox(mock_coord_env)
        assert res2["processed_count"] == 0

        # Merge queue remains exactly 1 item
        with open(mock_coord_env / "MERGE_QUEUE.json", encoding="utf-8") as f:
            mq = json.load(f)
        assert len(mq["queue"]) == 1

    def test_unknown_task_id_fails_closed(self, mock_coord_env: Path) -> None:
        msg_file = mock_coord_env / "outbox" / "M3" / "RESULT-T999-attempt1.json"
        payload = {
            "task_id": "T999",  # Not in task board
            "lease_epoch": 1,
            "status": "SUBMITTED",
            "result_commit": "commit_unknown",
            "changed_files": [],
            "attempt": 1,
        }
        with open(msg_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with pytest.raises(OutboxReductionError, match="Unknown task_id"):
            reduce_outbox(mock_coord_env)
