from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import create_p2wpkh_script_code
from jmwallet.wallet.psbt import parse_psbt
from jmwallet.wallet.signing import sign_p2wpkh_input

from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.tx_builder import TxBuilder


def _mock_wallet() -> Mock:
    wallet = Mock()

    private_key = PrivateKey(b"\x01" * 32)
    key = Mock()
    key.get_public_key_bytes.return_value = private_key.public_key.format(
        compressed=True,
    )

    wallet.get_key_for_address.return_value = key

    master_key = Mock()
    master_key.fingerprint = b"\x00\x00\x00\x00"
    wallet.master_key = master_key

    signing_plan = SimpleNamespace(
        signable_count=2,
        source_psbt=None,
    )
    wallet.prepare_psbt_signing.return_value = signing_plan

    default_signing_result = SimpleNamespace(
        psbt=None,
        signed_indices=[0, 1],
    )
    wallet.sign_psbt.return_value = default_signing_result

    def prepare_psbt_signing(psbt: bytes, scan_range: int) -> SimpleNamespace:
        current_plan = wallet.prepare_psbt_signing.return_value
        if current_plan is signing_plan:
            signing_plan.source_psbt = psbt
        return cast(SimpleNamespace, current_plan)

    def sign_psbt(plan: SimpleNamespace) -> SimpleNamespace:
        current_result = wallet.sign_psbt.return_value
        if current_result is default_signing_result:
            parsed = parse_psbt(plan.source_psbt)
            pubkey = wallet.get_key_for_address.return_value.get_public_key_bytes(
                compressed=True,
            )
            for index in range(len(parsed.input_maps)):
                signature = sign_p2wpkh_input(
                    parsed.transaction,
                    index,
                    create_p2wpkh_script_code(pubkey),
                    100_000,
                    private_key,
                )
                parsed.append_input_key_value(
                    index,
                    b"\x02" + pubkey,
                    signature,
                )
            default_signing_result.psbt = parsed.serialize()
        return cast(SimpleNamespace, current_result)

    wallet.prepare_psbt_signing.side_effect = prepare_psbt_signing
    wallet.sign_psbt.side_effect = sign_psbt

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
    assert psbt == wallet.sign_psbt.return_value.psbt


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


def test_build_and_sign_funding_tx_removes_empty_witness_script_records(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=(
            "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
        ),
        change_address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        wallet=wallet,
    )

    signing_plan = wallet.prepare_psbt_signing.return_value
    # The compact-size encoding of an empty PSBT_IN_WITNESS_SCRIPT record is
    # 01 05 00. Check the exact record encoding rather than importing
    # jmwallet's parser, because this regression must remain independent of
    # jmwallet's internal PSBT representation.
    assert b"\x01\x05\x00" not in signing_plan.source_psbt


def test_remove_empty_witness_scripts_rejects_invalid_psbt() -> None:
    with pytest.raises(ValueError, match="invalid PSBT magic"):
        TxBuilder._remove_empty_witness_scripts(b"not-a-psbt")


def test_remove_empty_witness_scripts_preserves_nonempty_witness_script() -> None:
    psbt = b"psbt\xff\x01\x05\x03abc\x00"

    assert TxBuilder._remove_empty_witness_scripts(psbt) == psbt


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


def test_build_and_sign_funding_tx_rejects_changed_input_metadata(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    def sign_psbt(plan: SimpleNamespace) -> SimpleNamespace:
        parsed = parse_psbt(plan.source_psbt)
        pubkey = wallet.get_key_for_address.return_value.get_public_key_bytes(
            compressed=True,
        )
        witness = bytearray(
            next(
                record.value
                for record in parsed.input_maps[0].records
                if record.key == b"\x01"
            )
        )
        witness[0] ^= 1
        parsed.input_maps[0].records = [
            replace(record, value=bytes(witness)) if record.key == b"\x01" else record
            for record in parsed.input_maps[0].records
        ]
        for index in range(len(parsed.input_maps)):
            parsed.append_input_key_value(
                index, b"\x02" + pubkey, b"\x30\x06\x02\x01\x01\x02\x01\x01\x01"
            )
        return SimpleNamespace(
            psbt=parsed.serialize(),
            signed_indices=[0, 1],
        )

    wallet.sign_psbt.side_effect = sign_psbt

    with pytest.raises(RuntimeError, match="changed PSBT input metadata"):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )


def test_build_and_sign_funding_tx_rejects_missing_partial_signature(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    def sign_psbt(signing_plan: SimpleNamespace) -> SimpleNamespace:
        parsed = parse_psbt(signing_plan.source_psbt)
        pubkey = wallet.get_key_for_address.return_value.get_public_key_bytes(
            compressed=True,
        )

        signature = sign_p2wpkh_input(
            parsed.transaction,
            0,
            create_p2wpkh_script_code(pubkey),
            100_000,
            PrivateKey(b"\x01" * 32),
        )
        parsed.append_input_key_value(
            0,
            b"\x02" + pubkey,
            signature,
        )

        # Deliberately leave input 1 unsigned.
        return SimpleNamespace(
            psbt=parsed.serialize(),
            signed_indices=[0, 1],
        )

    wallet.sign_psbt.side_effect = sign_psbt

    with pytest.raises(
        RuntimeError,
        match="JoinMarket wallet did not return exactly one signature for input 1",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )


def test_build_and_sign_funding_tx_rejects_invalid_partial_signature(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    def sign_psbt(signing_plan: SimpleNamespace) -> SimpleNamespace:
        parsed = parse_psbt(signing_plan.source_psbt)
        pubkey = wallet.get_key_for_address.return_value.get_public_key_bytes(
            compressed=True,
        )
        private_key = PrivateKey(b"\x01" * 32)
        for index in range(len(parsed.input_maps)):
            signature = bytearray(
                sign_p2wpkh_input(
                    parsed.transaction,
                    index,
                    create_p2wpkh_script_code(pubkey),
                    100_000,
                    private_key,
                )
            )
            signature[0] ^= 1
            parsed.append_input_key_value(
                index,
                b"\x02" + pubkey,
                bytes(signature),
            )
        return SimpleNamespace(
            psbt=parsed.serialize(),
            signed_indices=[0, 1],
        )

    wallet.sign_psbt.side_effect = sign_psbt

    with pytest.raises(
        RuntimeError,
        match="invalid P2WPKH signature for input 0",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )


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

    assert signed_psbt == wallet.sign_psbt.return_value.psbt
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


def test_build_and_sign_funding_tx_rejects_duplicate_signed_indices(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.sign_psbt.return_value = SimpleNamespace(
        psbt=b"psbt\xffsigned",
        signed_indices=[0, 0],
    )

    with pytest.raises(
        RuntimeError,
        match="duplicate signed input indices",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )


def test_build_and_sign_funding_tx_rejects_different_signed_psbt(
    classified_utxos: list[ClassifiedUTXO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.sign_psbt.side_effect = lambda plan: SimpleNamespace(
        psbt=b"psbt\xff\x00",
        signed_indices=[0, 1],
    )

    monkeypatch.setattr(
        "jmlightning.tx_builder.parse_psbt",
        lambda _: SimpleNamespace(unsigned_tx=b"different-transaction"),
    )

    with pytest.raises(
        RuntimeError,
        match="PSBT for a different transaction",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=(
                "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
            ),
            change_address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            wallet=wallet,
        )


def test_build_and_sign_funding_tx_rejects_invalid_signed_psbt(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.sign_psbt.side_effect = lambda plan: SimpleNamespace(
        psbt=b"not-a-psbt",
        signed_indices=[0, 1],
    )

    with pytest.raises(
        RuntimeError,
        match="invalid signed PSBT",
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


def test_build_and_sign_funding_tx_rejects_empty_inputs() -> None:
    builder = TxBuilder()

    plan = ExecutionPlan(
        inputs=[],
        amount=100_000,
        fee=100,
        vsize=100,
        change=0,
        warnings=[],
        rationale="test",
    )

    wallet = _mock_wallet()

    with pytest.raises(
        ValueError,
        match="at least one input",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()
    wallet.sign_psbt.assert_not_called()


def test_build_and_sign_funding_tx_rejects_duplicate_inputs(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()

    coin = classified_utxos[0]

    plan = ExecutionPlan(
        inputs=[coin, coin],
        amount=100_000,
        fee=100,
        vsize=100,
        change=99_900,
        warnings=[],
        rationale="test",
    )

    wallet = _mock_wallet()

    with pytest.raises(
        ValueError,
        match="duplicate inputs",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()
    wallet.sign_psbt.assert_not_called()


def test_build_and_sign_funding_tx_rejects_negative_fee(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()

    plan = _build_plan(classified_utxos)

    invalid_plan = ExecutionPlan(
        inputs=plan.inputs,
        amount=plan.amount,
        fee=-1,
        vsize=plan.vsize,
        change=plan.change,
        warnings=plan.warnings,
        rationale=plan.rationale,
    )

    wallet = _mock_wallet()

    with pytest.raises(
        ValueError,
        match="fee cannot be negative",
    ):
        builder.build_and_sign_funding_tx(
            plan=invalid_plan,
            funding_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()
    wallet.sign_psbt.assert_not_called()


def test_build_and_sign_funding_tx_rejects_inconsistent_amounts(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()

    plan = _build_plan(classified_utxos)

    invalid_plan = ExecutionPlan(
        inputs=plan.inputs,
        amount=plan.amount + 1,
        fee=plan.fee,
        vsize=plan.vsize,
        change=plan.change,
        warnings=plan.warnings,
        rationale=plan.rationale,
    )

    wallet = _mock_wallet()

    with pytest.raises(
        ValueError,
        match="inconsistent amounts",
    ):
        builder.build_and_sign_funding_tx(
            plan=invalid_plan,
            funding_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()
    wallet.sign_psbt.assert_not_called()


def test_build_and_sign_funding_tx_rejects_non_positive_amount(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)

    invalid_plan = ExecutionPlan(
        inputs=plan.inputs,
        amount=0,
        fee=plan.fee,
        vsize=plan.vsize,
        change=plan.change,
        warnings=plan.warnings,
        rationale=plan.rationale,
    )

    with pytest.raises(ValueError, match="amount must be positive"):
        builder.build_and_sign_funding_tx(
            plan=invalid_plan,
            funding_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
        )


def test_build_and_sign_funding_tx_rejects_negative_change(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)

    invalid_plan = ExecutionPlan(
        inputs=plan.inputs,
        amount=plan.amount,
        fee=plan.fee,
        vsize=plan.vsize,
        change=-1,
        warnings=plan.warnings,
        rationale=plan.rationale,
    )

    with pytest.raises(ValueError, match="change cannot be negative"):
        builder.build_and_sign_funding_tx(
            plan=invalid_plan,
            funding_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
        )
