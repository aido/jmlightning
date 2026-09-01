from typing import Any, cast

import pytest

from jmlightning.models import ClassifiedUTXO
from jmlightning.policy import Capability, Policy, PolicyEngine


def test_policy_allows_declared_capability() -> None:
    policy = Policy(
        {
            Capability.OPEN_CHANNEL,
            Capability.SWAP,
        }
    )

    assert policy.allows(Capability.OPEN_CHANNEL)
    assert policy.allows(Capability.SWAP)
    assert not policy.allows(Capability.SPLICE)


def test_policy_is_immutable() -> None:
    policy = Policy({Capability.OPEN_CHANNEL})

    with pytest.raises(AttributeError):
        cast(Any, policy.capabilities).add(Capability.SWAP)


def test_cj_out_can_open_channel() -> None:
    engine = PolicyEngine()

    assert engine.allows(
        "cj-out",
        Capability.OPEN_CHANNEL,
    )


def test_cj_change_cannot_open_channel() -> None:
    engine = PolicyEngine()

    assert not engine.allows(
        "cj-change",
        Capability.OPEN_CHANNEL,
    )


def test_cj_change_can_swap() -> None:
    engine = PolicyEngine()

    assert engine.allows(
        "cj-change",
        Capability.SWAP,
    )


def test_cj_out_can_be_remixed() -> None:
    engine = PolicyEngine()

    assert engine.allows(
        "cj-out",
        Capability.REMIX,
    )


def test_new_coin_cannot_open_channel() -> None:
    engine = PolicyEngine()

    assert not engine.allows(
        "new",
        Capability.OPEN_CHANNEL,
    )


def test_reserved_coin_has_no_capabilities() -> None:
    engine = PolicyEngine()

    for capability in Capability:
        assert not engine.allows(
            "reserved",
            capability,
        )


def test_filter_returns_only_allowed_coins(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    result = policy_engine.filter(
        classified_utxos,
        Capability.OPEN_CHANNEL,
    )

    assert [coin.status for coin in result] == [
        "cj-out",
    ]


def test_filter_preserves_input_order(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    result = policy_engine.filter(
        classified_utxos,
        Capability.SWAP,
    )

    assert [coin.status for coin in result] == [
        "cj-out",
        "cj-change",
        "deposit",
    ]


def test_filter_does_not_modify_input(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    original = list(classified_utxos)

    policy_engine.filter(
        classified_utxos,
        Capability.OPEN_CHANNEL,
    )

    assert classified_utxos == original


def test_reject_returns_disallowed_coins(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    result = policy_engine.reject(
        classified_utxos,
        Capability.OPEN_CHANNEL,
    )

    assert [coin.status for coin in result] == [
        "cj-change",
        "deposit",
        "reserved",
    ]


def test_reject_preserves_input_order(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    result = policy_engine.reject(
        classified_utxos,
        Capability.SWAP,
    )

    assert [coin.status for coin in result] == [
        "reserved",
    ]


def test_validate_accepts_allowed_coins(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    allowed = policy_engine.filter(
        classified_utxos,
        Capability.OPEN_CHANNEL,
    )

    policy_engine.validate(
        allowed,
        Capability.OPEN_CHANNEL,
    )


def test_validate_rejects_disallowed_coins(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    with pytest.raises(
        PermissionError,
        match="OPEN_CHANNEL",
    ):
        policy_engine.validate(
            classified_utxos,
            Capability.OPEN_CHANNEL,
        )


def test_validate_error_identifies_rejected_utxo(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    rejected = classified_utxos[1]

    with pytest.raises(
        PermissionError,
        match=rf"{rejected.utxo.txid}:{rejected.utxo.vout}",
    ):
        policy_engine.validate(
            classified_utxos,
            Capability.OPEN_CHANNEL,
        )


def test_validate_error_identifies_status(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    with pytest.raises(
        PermissionError,
        match=r"status: cj-change",
    ):
        policy_engine.validate(
            classified_utxos,
            Capability.OPEN_CHANNEL,
        )


def test_validate_accepts_empty_collection(
    policy_engine: PolicyEngine,
) -> None:
    policy_engine.validate(
        [],
        Capability.OPEN_CHANNEL,
    )


def test_reused_address_cannot_open_channel(
    policy_engine: PolicyEngine,
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    reused = ClassifiedUTXO(
        utxo=classified_utxos[0].utxo,
        status="reused",
    )

    allowed = policy_engine.filter(
        [reused],
        Capability.OPEN_CHANNEL,
    )

    assert allowed == []
