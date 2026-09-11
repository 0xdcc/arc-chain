"""C12-C16 protocol identity, exact V4 hashing and conservative capability boundaries."""

from dataclasses import replace
from hashlib import sha3_256
from typing import Any

import pytest

from arbitrage_contracts.identity import AssetRef, FeeModel, PoolDescriptor, PoolKey, TokenKey
from market_catalog.verification import (
    DEPLOYMENTS,
    DYNAMIC_FEE_FLAG,
    WETH_ADDRESS,
    ZERO_ADDRESS,
    compute_v4_pool_id,
    currency_asset,
    v3_fee_model,
    verify_pool,
    verify_v4_pool_id,
)

# Independent synthetic vector: five manually padded 32-byte words, Crypto.Hash.keccak.
# Deliberately no call to the production encoder/hash helper to construct this expectation.
VECTOR = "0x5fb36fcf81a62ac30e44924e98284b317a1fbfdd7b223b788319c663baebc930"
FACTORIES = {
    "uniswap-v2": "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
    "uniswap-v3": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
    "up-v3": "0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3",
    "ramses-v3": "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8",
    "uniswap-v4": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "giga-v3": "0xece6ecd61177336ea6fb9b17937ac439d85ee20b",
}


def pool(
    protocol: str = "uniswap-v4", *, hooks: str = ZERO_ADDRESS, fee: int = 3000
) -> PoolDescriptor:
    v4 = protocol == "uniswap-v4"
    c0 = ZERO_ADDRESS if v4 else WETH_ADDRESS
    c1 = WETH_ADDRESS if v4 else "0x" + "ff" * 20
    return PoolDescriptor(
        PoolKey(
            4663,
            protocol,
            "manager" if v4 else "factory",
            FACTORIES[protocol],
            "bytes32" if v4 else "address",
            compute_v4_pool_id(c0, c1, fee, 60, hooks) if v4 else "0x" + "cc" * 20,
        ),
        currency_asset(4663, c0),
        currency_asset(4663, c1),
        FeeModel.static(fee),
        tick_spacing=None if protocol == "uniswap-v2" else 60,
        hooks=hooks if v4 else None,
    )


def change_key(item: PoolDescriptor, **changes: Any) -> PoolDescriptor:
    fields = {
        name: getattr(item.key, name)
        for name in (
            "chain_id",
            "protocol_id",
            "venue_kind",
            "venue_address",
            "pool_id_kind",
            "pool_id",
        )
    }
    fields.update(changes)
    return replace(item, key=PoolKey(**fields))


def test_v4_independent_known_vector_and_not_sha3() -> None:
    assert compute_v4_pool_id(ZERO_ADDRESS, WETH_ADDRESS, 3000, 60, ZERO_ADDRESS) == VECTOR
    words = [0, int(WETH_ADDRESS, 16), 3000, 60, 0]
    abi_words = b"".join(n.to_bytes(32, "big") for n in words)
    assert "0x" + sha3_256(abi_words).hexdigest() != VECTOR
    assert verify_v4_pool_id(VECTOR, ZERO_ADDRESS, WETH_ADDRESS, 3000, 60, ZERO_ADDRESS)
    assert len(VECTOR) == 66


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency0", "0x" + "00" * 19 + "01"),
        ("currency1", "0x" + "ff" * 20),
        ("fee", 3001),
        ("tick_spacing", 61),
        ("hooks", "0x" + "00" * 19 + "01"),
    ],
)
def test_every_v4_key_field_bound(field: str, value: Any) -> None:
    args: dict[str, Any] = dict(
        currency0=ZERO_ADDRESS,
        currency1=WETH_ADDRESS,
        fee=3000,
        tick_spacing=60,
        hooks=ZERO_ADDRESS,
    )
    args[field] = value
    assert verify_v4_pool_id(VECTOR, **args) is False


def test_v4_currency_order_strict() -> None:
    rejected = False
    try:
        compute_v4_pool_id(WETH_ADDRESS, ZERO_ADDRESS, 3000, 60, ZERO_ADDRESS)
    except ValueError:
        rejected = True
    assert rejected, "Reversed currencies must raise, never silently swap"


def test_equal_currency_rejected() -> None:
    with pytest.raises(ValueError, match="currency0 < currency1"):
        compute_v4_pool_id(WETH_ADDRESS, WETH_ADDRESS, 3000, 60, ZERO_ADDRESS)


def test_address_case_has_no_effect_on_numeric_order_or_pool_id() -> None:
    assert (
        compute_v4_pool_id(ZERO_ADDRESS, "0x" + WETH_ADDRESS[2:].upper(), 3000, 60, ZERO_ADDRESS)
        == VECTOR
    )


@pytest.mark.parametrize(
    "bad",
    ["0x" + "ab" * 20, "0x" + "ab" * 31, "0x" + "ab" * 33, "0x" + "gg" * 32, VECTOR + "\n", None],
)
def test_v4_id_format_strict(bad: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        verify_v4_pool_id(bad, ZERO_ADDRESS, WETH_ADDRESS, 3000, 60, ZERO_ADDRESS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("fee", -1),
        ("fee", 1 << 24),
        ("fee", True),
        ("fee", 1.5),
        ("tick_spacing", 0),
        ("tick_spacing", -1),
        ("tick_spacing", 1 << 15),
        ("tick_spacing", -(1 << 23) - 1),
        ("tick_spacing", True),
        ("hooks", "0x0"),
        ("currency0", "0x0"),
        ("currency1", "malicious"),
    ],
)
def test_v4_parameter_bounds(field: str, value: Any) -> None:
    args: dict[str, Any] = dict(
        currency0=ZERO_ADDRESS,
        currency1=WETH_ADDRESS,
        fee=3000,
        tick_spacing=60,
        hooks=ZERO_ADDRESS,
    )
    args[field] = value
    with pytest.raises((ValueError, TypeError)):
        compute_v4_pool_id(**args)


def test_v4_hook_gate() -> None:
    result = verify_pool(pool(hooks="0x" + "00" * 19 + "01"))
    assert result.status == "unsupported", "Nonzero Hook must never become a standard pool"
    assert "HOOK_ADAPTER_REQUIRED" in result.reasons
    standard = verify_pool(pool())
    assert standard.status == "parameters_verified"
    assert standard.review_status == "pending_review" and standard.can_quote == "unknown"


@pytest.mark.parametrize("fee", [DYNAMIC_FEE_FLAG, DYNAMIC_FEE_FLAG | 3000])
def test_dynamic_flag_cannot_masquerade_as_static(fee: int) -> None:
    result = verify_pool(pool(fee=fee))
    assert result.status == "unsupported" and result.fee_model.kind == "dynamic"
    assert "DYNAMIC_FEE_ADAPTER_REQUIRED" in result.reasons
    assert result.can_atomic_execute == "unsupported"


@pytest.mark.parametrize("fee", [0, 1_000_000])
def test_v4_static_boundary_fees(fee: int) -> None:
    result = verify_pool(pool(fee=fee))
    assert result.status == "parameters_verified"
    assert result.fee_model.raw_value == fee and result.fee_model.kind == "static"


def test_v4_invalid_static_fee_rejected() -> None:
    assert verify_pool(pool(fee=1_000_001)).status == "rejected"


@pytest.mark.parametrize("raw,bps", [(0, 0), (100, 1), (500, 5), (3000, 30), (10000, 100)])
def test_standard_v3_fee_exact_ratio(raw: int, bps: int) -> None:
    model = v3_fee_model(raw)
    assert model.raw_value == raw and model.unit == "hundredths_of_bip"
    assert model.numerator is not None and model.denominator is not None
    assert model.numerator * 10000 == bps * model.denominator
    assert verify_pool(pool("uniswap-v3", fee=raw)).fee_model == model


@pytest.mark.parametrize("raw", [-1, 1_000_000, 1 << 24, True, 100.0])
def test_v3_invalid_fee(raw: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        v3_fee_model(raw)


@pytest.mark.parametrize(
    "protocol", ["uniswap-v2", "uniswap-v3", "up-v3", "ramses-v3", "uniswap-v4"]
)
def test_factory_protocol_alignment(protocol: str) -> None:
    item = pool(protocol)
    result = verify_pool(item)
    assert result.status in ("parameters_verified", "pending_review")
    assert "UNREGISTERED_PROTOCOL_OR_VENUE" not in result.reasons
    assert (4663, protocol, item.key.venue_kind, FACTORIES[protocol]) in DEPLOYMENTS
    impostor = change_key(item, venue_address="0x" + "dd" * 20)
    assert verify_pool(impostor).status == "rejected"


@pytest.mark.parametrize(
    "protocol",
    [
        "uniswap-v3-malicious",
        "fake/uniswap-v3",
        "Uniswap-V3",
        "uniswap_v3",
        " uniswap-v3",
        "alandale-cl",
        "uniswap-v4\n",
    ],
)
def test_unregistered_dex_and_malicious_alias_rejected(protocol: str) -> None:
    assert verify_pool(change_key(pool("uniswap-v3"), protocol_id=protocol)).status == "rejected"


def test_wrong_factory_for_other_registered_protocol_rejected() -> None:
    item = change_key(pool("uniswap-v3"), venue_address=FACTORIES["up-v3"])
    assert verify_pool(item).status == "rejected"


def test_giga_stays_authorization_pending() -> None:
    result = verify_pool(pool("giga-v3"))
    assert result.status == "pending_review"
    assert result.reasons == ("AUTH_EVIDENCE_PENDING",)
    assert result.review_status != "approved" and result.can_quote == "unknown"


@pytest.mark.parametrize("protocol", ["up-v3", "ramses-v3"])
def test_fork_units_not_inferred_from_v3_name(protocol: str) -> None:
    item = replace(pool(protocol, fee=53), fee_model=FeeModel.static(53, unit="bps"))
    result = verify_pool(item)
    assert result.status == "pending_review" and result.fee_model.kind == "unknown"
    assert result.reasons == ("FORK_FEE_UNIT_EVIDENCE_REQUIRED",)


def test_v2_fee_not_guessed_from_name_or_fee_method() -> None:
    result = verify_pool(pool("uniswap-v2"))
    assert result.status == "pending_review" and result.fee_model.raw_value is None
    assert result.reasons == ("V2_FEE_EVIDENCE_REQUIRED",)


@pytest.mark.parametrize(
    "fee",
    [
        FeeModel.static(3000, unit="bps"),
        FeeModel.static(3000, numerator=3, denominator=100),
        FeeModel.static(3000, numerator=3000),
        FeeModel("static", 3000, "hundredths_of_bip", hook_ref=ZERO_ADDRESS),
    ],
)
def test_fee_unit_ratio_or_dynamic_metadata_spoof_rejected(fee: FeeModel) -> None:
    assert verify_pool(replace(pool("uniswap-v3"), fee_model=fee)).status == "rejected"


@pytest.mark.parametrize(
    "fee,status", [(FeeModel.unknown(), "pending_review"), (FeeModel.dynamic(), "unsupported")]
)
def test_unknown_dynamic_never_static_default(fee: FeeModel, status: str) -> None:
    assert verify_pool(replace(pool("uniswap-v3"), fee_model=fee)).status == status


def test_native_weth_fake_weth_and_cross_chain_physically_distinct() -> None:
    native = currency_asset(4663, ZERO_ADDRESS)
    weth = currency_asset(4663, WETH_ADDRESS)
    fake_weth = currency_asset(4663, "0x" + "11" * 20)
    bsc_weth = currency_asset(56, WETH_ADDRESS)
    assert len({native, weth, fake_weth, bsc_weth}) == 4
    assert native.interface_kind == "native" and native.token_key is None
    assert weth.token_key == TokenKey(4663, WETH_ADDRESS)
    assert weth.interface_kind == "erc20"
    assert len({weth.token_key, fake_weth.token_key, bsc_weth.token_key}) == 3


def test_currency_replacement_changes_v4_pool_id() -> None:
    item = pool()
    changed = replace(item, currency0=currency_asset(4663, "0x" + "00" * 19 + "01"))
    assert verify_pool(changed).status == "rejected"
    assert verify_pool(changed).reasons == ("POOL_ID_MISMATCH",)


@pytest.mark.parametrize(
    "asset",
    [
        AssetRef.erc20(TokenKey(4663, ZERO_ADDRESS)),
        AssetRef.native(4663, "WETH"),
        AssetRef.native(4663, "ETH", "wrapped"),
    ],
)
def test_native_erc20_domain_spoof_rejected(asset: AssetRef) -> None:
    assert verify_pool(replace(pool(), currency0=asset)).status == "rejected"


def test_cross_chain_pool_and_assets_rejected() -> None:
    item = pool("uniswap-v3")
    with pytest.raises(ValueError, match="same chain"):
        replace(item, currency0=currency_asset(56, WETH_ADDRESS))
    key = PoolKey(56, "uniswap-v3", "factory", FACTORIES["uniswap-v3"], "address", item.key.pool_id)
    foreign = replace(
        item,
        key=key,
        currency0=currency_asset(56, WETH_ADDRESS),
        currency1=currency_asset(56, "0x" + "ff" * 20),
    )
    assert verify_pool(foreign).status == "rejected"


@pytest.mark.parametrize(
    "changes",
    [
        {"venue_kind": "factory"},
        {"pool_id_kind": "address", "pool_id": WETH_ADDRESS},
        {"pool_id": "0x" + "ab" * 32},
    ],
)
def test_v4_wrong_locator_rejected(changes: dict[str, Any]) -> None:
    assert verify_pool(change_key(pool(), **changes)).status == "rejected"


@pytest.mark.parametrize("field", ["tick_spacing", "hooks"])
def test_missing_v4_parameters_pending(field: str) -> None:
    changes: dict[str, Any] = {field: None}
    assert verify_pool(replace(pool(), **changes)).status == "pending_review"


@pytest.mark.parametrize("status", ["pending", "destroyed", "unknown"])
def test_unconfirmed_deployment_never_parameters_verified(status: str) -> None:
    assert verify_pool(replace(pool(), deployment_status=status)).status == "pending_review"


def test_reversed_pool_tokens_rejected() -> None:
    item = pool("uniswap-v3")
    assert (
        verify_pool(replace(item, currency0=item.currency1, currency1=item.currency0)).status
        == "rejected"
    )


def test_fabricated_evidence_refs_do_not_approve_or_prove_profit() -> None:
    result = verify_pool(replace(pool(), identity_evidence_refs=("live_verified:fake",)))
    assert result.status == "parameters_verified" and result.review_status == "pending_review"
    assert result.can_quote == result.can_simulate == "unknown"
    assert result.can_atomic_execute == "unsupported"
    assert "SOURCE_AUTHENTICATION_REQUIRED" in result.reasons


@pytest.mark.parametrize(
    "protocol,changes",
    [
        ("uniswap-v2", {"tick_spacing": 60}),
        ("uniswap-v2", {"fee_model": FeeModel.static(1 << 24)}),
        ("uniswap-v3", {"tick_spacing": 0}),
        ("uniswap-v3", {"tick_spacing": -1}),
        ("uniswap-v3", {"tick_spacing": 1 << 23}),
        ("uniswap-v3", {"hooks": ZERO_ADDRESS}),
    ],
)
def test_v2_v3_parameter_bounds(protocol: str, changes: dict[str, Any]) -> None:
    assert verify_pool(replace(pool(protocol), **changes)).status == "rejected"


def test_v3_native_currency_and_zero_pool_rejected() -> None:
    item = pool("uniswap-v3")
    assert (
        verify_pool(replace(item, currency0=currency_asset(4663, ZERO_ADDRESS))).status
        == "rejected"
    )
    assert verify_pool(change_key(item, pool_id=ZERO_ADDRESS)).status == "rejected"
