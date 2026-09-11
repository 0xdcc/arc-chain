"""Coordination & Dispatch Engine Tests (T04)

Verifies:
- Dispatch order validation and lease disjointness
- Outbox message reduction, idempotence, and lease release
- Rejection of overlapping leases, forbidden capabilities, path traversal, and unauthorized file writes
"""

import json
import tempfile
from pathlib import Path
import pytest

from tools.coord.reduce_outbox import OutboxReductionError, reduce_outbox
from tools.coord.validate_dispatch import DispatchValidationError, validate_dispatch


class TestCoordinationDispatch:
    """Test suite for T04 dispatch validation and outbox reduction."""

    def test_validate_dispatch_success(self) -> None:
        dispatch = {
            "plan_id": "ARC-4B-v3.1-20260911-13817f4",
            "task_id": "T07",
            "attempt": 1,
            "lease_epoch": 1,
            "lead_brain": "M2",
            "worker_id": "M2-worker-1",
            "baseline_sha": "6d9ed14",
            "contract_digest": "604333c5",
            "exact_write_allowlist": [
                "arc_readiness/network.py",
                "arc_readiness/rpc_readonly.py",
            ],
            "requested_capabilities": ["read_only_rpc"],
        }
        active_leases = {
            "T13": {
                "lease_epoch": 1,
                "exact_write_allowlist": ["arc_markets/deployments.py"],
            }
        }
        valid, err = validate_dispatch(dispatch, active_leases)
        assert valid
        assert err is None

    def test_validate_dispatch_overlapping_lease_rejected(self) -> None:
        dispatch = {
            "plan_id": "ARC-4B-v3.1-20260911-13817f4",
            "task_id": "T08",
            "attempt": 1,
            "lease_epoch": 1,
            "lead_brain": "M2",
            "worker_id": "M2-worker-2",
            "baseline_sha": "6d9ed14",
            "contract_digest": "604333c5",
            "exact_write_allowlist": [
                "arc_readiness/fixed_block.py",
                "arc_readiness/network.py",  # overlap with active T07 lease
            ],
        }
        active_leases = {
            "T07": {
                "lease_epoch": 1,
                "exact_write_allowlist": ["arc_readiness/network.py"],
            }
        }
        with pytest.raises(DispatchValidationError, match="File collision with active lease T07"):
            validate_dispatch(dispatch, active_leases)

    def test_validate_dispatch_forbidden_capability_rejected(self) -> None:
        dispatch = {
            "plan_id": "ARC-4B-v3.1-20260911-13817f4",
            "task_id": "T07",
            "attempt": 1,
            "lease_epoch": 1,
            "lead_brain": "M2",
            "worker_id": "M2-worker-1",
            "baseline_sha": "6d9ed14",
            "contract_digest": "604333c5",
            "exact_write_allowlist": ["arc_readiness/network.py"],
            "requested_capabilities": ["sign", "broadcast"],  # forbidden!
        }
        with pytest.raises(DispatchValidationError, match="Dispatch requests forbidden capabilities"):
            validate_dispatch(dispatch, {})

    def test_validate_dispatch_path_traversal_rejected(self) -> None:
        dispatch = {
            "plan_id": "ARC-4B-v3.1-20260911-13817f4",
            "task_id": "T07",
            "attempt": 1,
            "lease_epoch": 1,
            "lead_brain": "M2",
            "worker_id": "M2-worker-1",
            "baseline_sha": "6d9ed14",
            "contract_digest": "604333c5",
            "exact_write_allowlist": ["../escape.py"],
        }
        with pytest.raises(DispatchValidationError, match="Path traversal '..' forbidden"):
            validate_dispatch(dispatch, {})

    def test_reduce_outbox_success_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coord_test_") as tmpdir:
            c_root = Path(tmpdir)
            outbox_m2 = c_root / "outbox" / "M2"
            outbox_m2.mkdir(parents=True)

            task_board = {
                "tasks": {
                    "T07": {
                        "status": "LEASED",
                        "result": None,
                    }
                }
            }
            (c_root / "TASK_BOARD.json").write_text(json.dumps(task_board))

            leases = {
                "active_leases": {
                    "T07": {
                        "lease_epoch": 1,
                        "exact_write_allowlist": ["arc_readiness/network.py"],
                    }
                }
            }
            (c_root / "LEASES.json").write_text(json.dumps(leases))

            merge_queue = {"queue": [], "processed_messages": []}
            (c_root / "MERGE_QUEUE.json").write_text(json.dumps(merge_queue))

            result_payload = {
                "task_id": "T07",
                "attempt": 1,
                "lease_epoch": 1,
                "status": "SUBMITTED",
                "result_commit": "abc1234",
                "changed_files": ["arc_readiness/network.py"],
            }
            (outbox_m2 / "RESULT-T07-attempt1.json").write_text(json.dumps(result_payload))

            # First run: process message
            res1 = reduce_outbox(c_root)
            assert res1["processed_count"] == 1
            assert res1["queue_length"] == 1

            # Verify lease released and task status updated
            updated_leases = json.loads((c_root / "LEASES.json").read_text())
            assert "T07" not in updated_leases["active_leases"]

            updated_board = json.loads((c_root / "TASK_BOARD.json").read_text())
            assert updated_board["tasks"]["T07"]["status"] == "SUBMITTED"
            assert updated_board["tasks"]["T07"]["result"] == "abc1234"

            # Second run: idempotent, processes 0 new messages
            res2 = reduce_outbox(c_root)
            assert res2["processed_count"] == 0
            assert res2["queue_length"] == 1

    def test_reduce_outbox_unauthorized_write_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coord_test_fail_") as tmpdir:
            c_root = Path(tmpdir)
            outbox_m2 = c_root / "outbox" / "M2"
            outbox_m2.mkdir(parents=True)

            task_board = {"tasks": {"T07": {"status": "LEASED", "result": None}}}
            (c_root / "TASK_BOARD.json").write_text(json.dumps(task_board))

            leases = {
                "active_leases": {
                    "T07": {
                        "lease_epoch": 1,
                        "exact_write_allowlist": ["arc_readiness/network.py"],
                    }
                }
            }
            (c_root / "LEASES.json").write_text(json.dumps(leases))

            merge_queue = {"queue": [], "processed_messages": []}
            (c_root / "MERGE_QUEUE.json").write_text(json.dumps(merge_queue))

            result_payload = {
                "task_id": "T07",
                "attempt": 1,
                "lease_epoch": 1,
                "status": "SUBMITTED",
                "result_commit": "badcommit",
                "changed_files": [
                    "arc_readiness/network.py",
                    "arbitrage_contracts/unauthorized.py",  # unauthorized file!
                ],
            }
            (outbox_m2 / "RESULT-T07-attempt1.json").write_text(json.dumps(result_payload))

            with pytest.raises(OutboxReductionError, match="wrote file outside lease allowlist"):
                reduce_outbox(c_root)
