"""Synthetic codehash policy counterexamples; no live deployment claims."""
from collections.abc import Sequence
from typing import Any

import pytest
from eth_abi import encode
from web3 import Web3

from arc_readiness.errors import ArcValidationError
from arc_readiness.permit2 import CANONICAL_PERMIT2, check_permit2_allowance

OWNER = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
SPENDER = Web3.to_checksum_address("0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
PERMIT2 = Web3.to_checksum_address(CANONICAL_PERMIT2)
GOOD = "0x" + Web3.keccak(b"\x01").hex()
BAD = "0x" + "00" * 32


def invoke(policy: Any, *, check_code: bool = True) -> tuple[bool, list[str]]:
    calls: list[str] = []

    def rpc(method: str, params: Sequence[Any]) -> str:
        calls.append(method)
        assert params[1] == "0xa"
        if method == "eth_getCode":
            return "0x01"
        assert method == "eth_call"
        return "0x" + encode(["uint160", "uint48", "uint48"], [100, 101, 0]).hex()

    try:
        result = check_permit2_allowance(
            transport=rpc, owner=OWNER, token=TOKEN, spender=SPENDER,
            permit2_address=PERMIT2, requested_amount=100,
            block_context={"number": 10, "timestamp": 100},
            check_code=check_code, expected_digests=policy,
        )
    except ArcValidationError as exc:
        exc.probe_calls = calls  # type: ignore[attr-defined]
        raise
    return result.allowed, calls


@pytest.mark.parametrize("target", [SPENDER, PERMIT2])
@pytest.mark.parametrize("checksum", [False, True])
@pytest.mark.parametrize("wrong", [False, True])
def test_single_target_digest_binding(target: str, checksum: bool, wrong: bool) -> None:
    policy = {
        (addr if checksum else addr.lower()): (BAD if wrong and addr == target else GOOD)
        for addr in (SPENDER, PERMIT2)
    }
    if wrong:
        with pytest.raises(ArcValidationError, match="Bytecode hash mismatch"):
            invoke(policy)
    else:
        assert invoke(policy)[0] is True


@pytest.mark.parametrize("policy", [
    {}, {SPENDER: GOOD}, {PERMIT2: GOOD},
    {SPENDER: GOOD, PERMIT2: "0x01"},
    {SPENDER: GOOD, PERMIT2: "0x" + "gg" * 32},
    {SPENDER: GOOD, PERMIT2: GOOD, SPENDER.lower(): BAD},
    {SPENDER: GOOD, PERMIT2: GOOD, "invalid": GOOD},
])
def test_invalid_policy_rejected_before_rpc(policy: dict[str, str]) -> None:
    with pytest.raises(ArcValidationError) as err:
        invoke(policy)
    assert err.value.probe_calls == []  # type: ignore[attr-defined]


def test_policy_cannot_be_ignored_when_code_check_disabled() -> None:
    with pytest.raises(ArcValidationError, match="check_code") as err:
        invoke({SPENDER: GOOD, PERMIT2: GOOD}, check_code=False)
    assert err.value.probe_calls == []  # type: ignore[attr-defined]


def test_identical_case_duplicate_is_idempotent() -> None:
    assert invoke({SPENDER: GOOD, SPENDER.lower(): GOOD, PERMIT2: GOOD})[0] is True


def test_no_policy_retains_optional_behavior() -> None:
    assert invoke(None, check_code=False)[0] is True
