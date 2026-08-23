from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.tx_builder import TxBuilder


def _mock_wallet() -> Mock:
    wallet = Mock()

    key = Mock()
    key.get_public_key_bytes.return_value = b"\x02" + b"\x11" * 32

    wallet.get_key_for_address.return_value = key

    master_key = Mock()
    master_key.fingerprint = b"\x00\x00\x00\x00"
    wallet.master_key = master_key

    signing_plan = SimpleNamespace(
        signable_count=2,
    )
    wallet.prepare_psbt_signing.return_value = signing_plan

    wallet.sign_psbt.return_value = SimpleNamespace(
        psbt=b"psbt\xffsigned",
        signed_indices=[0, 1],
    )

    return wallet


def _build_plan(
    classified_utxos: list[ClassifiedUTXO],
) -> ExecutionPlan:
    planner = Planner()

    return planner.build_plan(
        selected_coins=classified_utxos[:2],
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )


def test_build_and_sign_funding_tx_creates_funding_output(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    tx, txid, funding_vout, psbt = builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=(
            "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
        ),
        change_address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        wallet=wallet,
    )

    assert funding_vout == 0
    assert len(tx.outputs) == 2
    assert tx.outputs[0].value == plan.amount
    assert tx.outputs[1].value == plan.change
    assert len(tx.inputs) == len(plan.inputs)
    assert txid
    assert psbt == b"psbt\xffsigned"


def test_build_and_sign_funding_tx_uses_psbt_signing(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        wallet=wallet,
    )

    wallet.prepare_psbt_signing.assert_called_once()
    wallet.sign_psbt.assert_called_once()

    signing_plan = wallet.prepare_psbt_signing.return_value
    wallet.sign_psbt.assert_called_once_with(signing_plan)


def test_build_and_sign_funding_tx_does_not_double_sign_inputs(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        wallet=wallet,
    )

    wallet.sign_input.assert_not_called()


def test_build_and_sign_funding_tx_returns_signed_psbt(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    _, _, _, signed_psbt = builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        wallet=wallet,
    )

    assert signed_psbt == b"psbt\xffsigned"
    assert signed_psbt.startswith(b"psbt\xff")


def test_build_and_sign_funding_tx_returns_correct_txid(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    tx, txid, _, _ = builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        wallet=wallet,
    )

    assert txid == builder._txid(tx)


def test_build_and_sign_funding_tx_omits_zero_change_output(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()
    builder = TxBuilder()

    selected = classified_utxos[:2]

    initial_plan = planner.build_plan(
        selected_coins=selected,
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    target_amount = 200_000 - initial_plan.fee

    plan = planner.build_plan(
        selected_coins=selected,
        target_amount=target_amount,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    assert plan.change == 0

    wallet = _mock_wallet()

    tx, _, funding_vout, _ = builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        wallet=wallet,
    )

    assert len(tx.outputs) == 1
    assert funding_vout == 0
    assert tx.outputs[0].value == plan.amount


def test_build_and_sign_funding_tx_rejects_incomplete_wallet_selection(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.prepare_psbt_signing.return_value = SimpleNamespace(
        signable_count=len(plan.inputs) - 1,
    )

    with pytest.raises(
        RuntimeError,
        match="did not identify all funding inputs",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )

    wallet.sign_psbt.assert_not_called()


def test_build_and_sign_funding_tx_rejects_incomplete_signing(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.sign_psbt.return_value = SimpleNamespace(
        psbt=b"psbt\xffsigned",
        signed_indices=[0],
    )

    with pytest.raises(
        RuntimeError,
        match="did not sign all funding inputs",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )


def test_build_and_sign_funding_tx_propagates_psbt_signing_failure(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.sign_psbt.side_effect = RuntimeError("signing failed")

    with pytest.raises(RuntimeError, match="signing failed"):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )

    wallet.sign_input.assert_not_called()
