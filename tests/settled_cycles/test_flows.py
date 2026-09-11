"""Action graph and balance-vector regression tests for W3-C."""

from __future__ import annotations

import unittest
from typing import Any

from research.settled_cycles.evidence import EvidenceBundle
from research.settled_cycles.flows import reconstruct_actions_and_flows
from research.settled_cycles.models import ActionKind, SubjectBalanceDelta

ORIGIN = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
POOL_A = "0x" + "aa" * 20
POOL_B = "0x" + "bb" * 20
WETH = "0x" + "cc" * 20
USDC = "0x" + "dd" * 20
USER_A = "0x" + "ee" * 20
USER_B = "0x" + "ff" * 20

SWAP_V3_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V4_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c4660300e274e944097073e"
WITHDRAWAL_TOPIC = "0x7fcf532c1207038a823a0c05291be65d6449741add6e94066936c4f63f5097a8"
POOL_ID = "0x" + "66" * 32


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def word(value: int, bits: int = 256) -> str:
    mask256 = (1 << 256) - 1
    val = (1 << 256) + value if value < 0 else value
    return f"{val & mask256:064x}"


def transfer(token: str, sender: str, recipient: str, value: int, index: int) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, address_topic(sender), address_topic(recipient)],
        "data": "0x" + word(value),
        "logIndex": index,
        "removed": False,
    }


def bundle(logs: list[dict[str, Any]], trace: dict[str, Any] | None = None, status: int = 1) -> EvidenceBundle:
    return EvidenceBundle(
        chain_id=4663,
        tx_hash="0x" + "12" * 32,
        block_header={"hash": "0x" + "34" * 32},
        transaction={"from": ORIGIN, "hash": "0x" + "12" * 32},
        receipt={"status": status, "logs": logs},
        trace=trace,
        evidence_metadata={"source_sha256": "0" * 64},
    )


def swap_log(pool: str, index: int) -> dict[str, Any]:
    return {
        "address": pool,
        "topics": [SWAP_V3_TOPIC, address_topic(ROUTER), address_topic(ROUTER)],
        "data": "0x" + word(-1) + word(2) + word(0) + word(0) + word(0),
        "logIndex": index,
        "removed": False,
    }


class FlowReconstructionTests(unittest.TestCase):
    def test_complex_topology_wrap_and_pool_reuse(self) -> None:
        logs = [
            {"address": WETH, "topics": [DEPOSIT_TOPIC, address_topic(ROUTER)], "data": "0x" + word(10), "logIndex": 0, "removed": False},
            swap_log(POOL_A, 1),
            swap_log(POOL_B, 2),
            swap_log(POOL_A, 3),
            {"address": WETH, "topics": [WITHDRAWAL_TOPIC, address_topic(ROUTER)], "data": "0x" + word(4), "logIndex": 4, "removed": False},
        ]
        actions, _ = reconstruct_actions_and_flows(bundle(logs))
        self.assertEqual(5, len(actions))
        self.assertEqual(
            [ActionKind.WRAP, ActionKind.SWAP, ActionKind.SWAP, ActionKind.SWAP, ActionKind.UNWRAP],
            [item.action_kind for item in actions],
        )
        self.assertEqual([1, 2, 3, 4, 5], [item.step_id for item in actions])
        self.assertEqual(POOL_A.lower(), actions[1].pool_key.pool_id.lower())
        self.assertEqual(POOL_A.lower(), actions[3].pool_key.pool_id.lower())
        self.assertEqual("native", actions[0].asset_in.interface_kind)
        self.assertEqual("erc20", actions[0].asset_out.interface_kind)

    def test_v4_pool_identity_is_bytes32(self) -> None:
        data = "0x" + word(-1, 128) + word(2, 128) + word(0) + word(0) + word(0) + word(500)
        log = {
            "address": ROUTER,
            "topics": [V4_TOPIC, POOL_ID, address_topic(ROUTER)],
            "data": data,
            "logIndex": 0,
            "removed": False,
        }
        actions, _ = reconstruct_actions_and_flows(bundle([log]))
        self.assertEqual(ActionKind.SWAP, actions[0].action_kind)
        self.assertEqual("manager", actions[0].pool_key.venue_kind)
        self.assertEqual(POOL_ID.lower(), actions[0].pool_key.pool_id.lower())

    def test_reverted_child_call_pruned(self) -> None:
        success_transfer = transfer(USDC, USER_A, USER_B, 100, 0)
        reverted_transfer = transfer(USDC, USER_A, USER_B, 70, 1)
        trace = {
            "from": ORIGIN,
            "to": ROUTER,
            "type": "CALL",
            "value": "0x0",
            "calls": [
                {
                    "type": "CALL",
                    "from": ROUTER,
                    "to": POOL_A,
                    "value": "0x5",
                    "error": "execution reverted",
                    "logs": [reverted_transfer],
                }
            ],
            "logs": [success_transfer],
        }
        actions, deltas = reconstruct_actions_and_flows(bundle([success_transfer, reverted_transfer], trace))
        self.assertEqual(2, len(actions))
        self.assertEqual(["success", "reverted"], [item.execution_status for item in actions])
        self.assertEqual(["log:0", "log:1"], [item.evidence_refs[0] for item in actions])
        self.assertEqual(100, sum(item.delta_atoms for item in deltas if item.subject_address == USER_B))
        self.assertEqual(-100, next(item.delta_atoms for item in deltas if item.subject_address == USER_A))

    def test_tx_level_revert_clears_all_token_deltas(self) -> None:
        log = transfer(USDC, USER_A, USER_B, 100, 0)
        actions, deltas = reconstruct_actions_and_flows(bundle([log], status=0))
        self.assertEqual("reverted", actions[0].execution_status)
        self.assertEqual((), deltas)

    def test_delegatecall_no_native_value_transfer(self) -> None:
        trace = {
            "from": ORIGIN,
            "to": ROUTER,
            "type": "CALL",
            "value": "0x0",
            "calls": [{"from": ORIGIN, "to": POOL_A, "type": "DELEGATECALL", "value": "0x64"}],
        }
        actions, deltas = reconstruct_actions_and_flows(bundle([], trace))
        self.assertEqual((), actions)
        self.assertEqual((), deltas)

    def test_trace_native_value_transfers_are_retained(self) -> None:
        trace = {"from": ORIGIN, "to": ROUTER, "type": "CALL", "value": "0x64"}
        actions, deltas = reconstruct_actions_and_flows(bundle([], trace))
        self.assertEqual(ActionKind.TRANSFER, actions[0].action_kind)
        self.assertEqual(100, actions[0].amount_in_atoms)
        self.assertIsNone(actions[0].trace_address)
        self.assertEqual({"success"}, {item.execution_status for item in actions})
        self.assertEqual(-100, next(item.delta_atoms for item in deltas if item.subject_address == ORIGIN))
        self.assertEqual(100, next(item.delta_atoms for item in deltas if item.subject_address == ROUTER))

    def test_self_transfer_delta_zero(self) -> None:
        log = transfer(USDC, USER_A, USER_A, 80, 0)
        actions, deltas = reconstruct_actions_and_flows(bundle([log]))
        self.assertEqual(ActionKind.TRANSFER, actions[0].action_kind)
        self.assertEqual(80, actions[0].amount_in_atoms)
        self.assertEqual(0, deltas[0].delta_atoms)

    def test_tx_level_delta_aggregation_conserves_value(self) -> None:
        logs = [
            transfer(USDC, USER_A, USER_B, 100, 0),
            transfer(USDC, USER_B, ORIGIN, 40, 1),
            transfer(USDC, ORIGIN, USER_B, 15, 2),
            transfer(WETH, USER_A, ORIGIN, 7, 3),
        ]
        _, deltas = reconstruct_actions_and_flows(bundle(logs))

        def delta_sort_key(item: SubjectBalanceDelta) -> tuple[str, str]:
            assert item.asset.token_key is not None
            return (item.subject_address, item.asset.token_key.address.lower())

        grouped: dict[tuple[str, str], int] = {}
        for item in deltas:
            assert item.asset.token_key is not None
            token = item.asset.token_key.address.lower()
            grouped[(item.subject_address, token)] = grouped.get((item.subject_address, token), 0) + item.delta_atoms
        self.assertEqual(-100, grouped[(USER_A, USDC)])
        self.assertEqual(75, grouped[(USER_B, USDC)])
        self.assertEqual(25, grouped[(ORIGIN, USDC)])
        self.assertEqual(-7, grouped[(USER_A, WETH)])
        self.assertEqual(7, grouped[(ORIGIN, WETH)])
        for token in (USDC, WETH):
            self.assertEqual(0, sum(value for (_, asset_token), value in grouped.items() if asset_token == token))
        self.assertEqual(sorted(deltas, key=delta_sort_key), list(deltas))

    def test_removed_logs_are_excluded(self) -> None:
        log = transfer(USDC, USER_A, USER_B, 100, 0)
        actions, deltas = reconstruct_actions_and_flows(bundle([{**log, "removed": True}]))
        self.assertEqual((), actions)
        self.assertEqual((), deltas)


if __name__ == "__main__":
    unittest.main()
