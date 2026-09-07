from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import (
    BIP32Derivation,
    ParsedTransaction,
    PSBTInput,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    create_psbt,
    parse_derivation_path,
    serialize_transaction,
)
from jmwallet.wallet.psbt import PSBT_IN_PARTIAL_SIG, parse_psbt
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

    def prepare_psbt_signing(psbt: bytes, scan_range: object) -> SimpleNamespace:
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
            signed_indices = []

            for index in range(len(parsed.input_maps)):
                input_map = parsed.input_maps[index]
                if not any(
                    record.key[1:] == pubkey
                    for record in input_map.records
                    if record.key[:1] == b"\x06"
                ):
                    continue

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
                signed_indices.append(index)

            default_signing_result.signed_indices = signed_indices
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


def _build_splice_psbt() -> bytes:
    tx_input = TxInput.from_hex(
        txid="aa" * 32,
        vout=1,
        sequence=0xFFFFFFFE,
        value=200_000,
        scriptpubkey="0014" + "11" * 20,
    )
    tx_output = TxOutput.from_address(
        "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        200_000,
    )
    psbt = create_psbt(
        version=2,
        inputs=[tx_input],
        outputs=[tx_output],
        locktime=0,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=200_000,
                witness_utxo_script=tx_input.scriptpubkey,
                witness_script=b"",
                sighash_type=1,
            )
        ],
    )
    parsed = parse_psbt(psbt)
    parsed.input_maps[0].append(b"\xfccln", b"input metadata")
    parsed.output_maps[0].append(b"\xfccln", b"output metadata")
    return parsed.serialize()


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


def test_build_and_sign_funding_tx_rejects_key_script_mismatch(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    bad_coin = replace(
        plan.inputs[0],
        utxo=replace(
            plan.inputs[0].utxo,
            scriptpubkey="0014" + "11" * 20,
        ),
    )
    bad_plan = replace(
        plan,
        inputs=[bad_coin, *plan.inputs[1:]],
    )

    with pytest.raises(
        RuntimeError,
        match="key does not match funding input script",
    ):
        builder.build_and_sign_funding_tx(
            plan=bad_plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )

    wallet.sign_psbt.assert_not_called()


def test_build_and_sign_funding_tx_rejects_boolean_signed_index(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()
    wallet.sign_psbt.return_value = SimpleNamespace(
        psbt=b"psbt\xffsigned",
        signed_indices=[True, 1],
    )

    with pytest.raises(
        RuntimeError,
        match="invalid signed input indices",
    ):
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
        match="did not return exactly one signature for input 1",
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
        match="did not identify all inputs",
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
        match="did not sign all inputs",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
            wallet=wallet,
        )


def test_build_and_sign_tx_supports_non_wallet_inputs(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()

    jm_inputs = classified_utxos[:2]
    signing_inputs = {
        1: jm_inputs[0],
        2: jm_inputs[1],
    }

    tx_inputs = [
        TxInput.from_hex(
            txid="2222222222222222222222222222222222222222222222222222222222222222",
            vout=0,
            sequence=0xFFFFFFFF,
            value=100_000,
            scriptpubkey="0014" + "11" * 20,
        ),
        *[
            TxInput.from_hex(
                txid=coin.utxo.txid,
                vout=coin.utxo.vout,
                sequence=0xFFFFFFFF,
                value=coin.utxo.value,
                scriptpubkey=coin.utxo.scriptpubkey,
            )
            for coin in jm_inputs
        ],
    ]

    tx = ParsedTransaction(
        version=2,
        inputs=tx_inputs,
        outputs=[
            TxOutput.from_address(
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                200_000,
            )
        ],
        witnesses=[[] for _ in tx_inputs],
        locktime=0,
        has_witness=True,
    )

    psbt_inputs = [
        PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=bytes.fromhex("0014" + "11" * 20),
            witness_script=b"",
        ),
    ]

    for coin in jm_inputs:
        key = wallet.get_key_for_address(coin.utxo.address)
        assert key is not None
        expected_pubkey = key.get_public_key_bytes(compressed=True)

        psbt_inputs.append(
            PSBTInput(
                witness_utxo_value=coin.utxo.value,
                witness_utxo_script=bytes.fromhex(
                    coin.utxo.scriptpubkey,
                ),
                witness_script=b"",
                sighash_type=1,
                bip32_derivations=[
                    BIP32Derivation(
                        pubkey=expected_pubkey,
                        fingerprint=wallet.master_key.fingerprint,
                        path=parse_derivation_path(coin.utxo.path),
                    )
                ],
            )
        )

    signed_psbt = builder._build_and_sign_tx(
        tx=tx,
        signing_inputs=signing_inputs,
        psbt_inputs=psbt_inputs,
        wallet=wallet,
    )[2]

    parsed = parse_psbt(signed_psbt)

    assert len(parsed.input_maps) == 3

    # The non-wallet input must remain unsigned.
    non_wallet_signatures = [
        record
        for record in parsed.input_maps[0].records
        if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
    ]
    assert non_wallet_signatures == []

    # Both JoinMarket inputs must have exactly one valid signature.
    for index, coin in signing_inputs.items():
        key = wallet.get_key_for_address(coin.utxo.address)
        assert key is not None
        expected_pubkey = key.get_public_key_bytes(compressed=True)
        signature_key = bytes([PSBT_IN_PARTIAL_SIG]) + expected_pubkey

        signatures = [
            record
            for record in parsed.input_maps[index].records
            if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
        ]

        assert len(signatures) == 1
        assert signatures[0].key == signature_key


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


def test_build_and_sign_funding_tx_rejects_unexpected_signed_input(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_plan(classified_utxos)
    wallet = _mock_wallet()

    wallet.sign_psbt.return_value = SimpleNamespace(
        psbt=b"psbt\xffsigned",
        signed_indices=[0, 2],
    )

    with pytest.raises(
        RuntimeError,
        match="did not sign exactly the inputs",
    ):
        builder.build_and_sign_funding_tx(
            plan=plan,
            funding_address=(
                "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
            ),
            change_address=("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"),
            wallet=wallet,
        )

    wallet.sign_psbt.assert_called_once()


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


def test_add_splice_in_input_preserves_cln_psbt_metadata(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    original = parse_psbt(_build_splice_psbt())

    updated = builder.add_splice_in_input(
        psbt=original.serialize(),
        coin=classified_utxos[2],
        relative_amount=40_000,
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )
    parsed = parse_psbt(updated)

    assert len(parsed.transaction.inputs) == 2
    assert len(parsed.transaction.outputs) == 2
    assert parsed.transaction.inputs[0] == original.transaction.inputs[0]
    assert parsed.input_maps[0].records == original.input_maps[0].records
    assert parsed.output_maps[0].records == original.output_maps[0].records
    assert parsed.transaction.inputs[1].txid == classified_utxos[2].utxo.txid
    assert parsed.transaction.inputs[1].vout == classified_utxos[2].utxo.vout
    assert parsed.transaction.outputs[0] == original.transaction.outputs[0]
    assert parsed.transaction.outputs[1].value == 60_000

    input_records = parsed.input_maps[1].records
    assert [record.key[:1] for record in input_records] == [
        b"\x01",
        b"\x03",
        b"\x06",
    ]
    assert parsed.output_maps[1].records == []

    assert parsed.unsigned_tx == serialize_transaction(
        parsed.transaction.version,
        parsed.transaction.inputs,
        parsed.transaction.outputs,
        parsed.transaction.locktime,
    )


@pytest.mark.parametrize(
    ("relative_amount", "error"),
    [
        (0, "Splice-in amount must be positive"),
        (-1, "Splice-in amount must be positive"),
        (100_001, "smaller than the requested splice-in amount"),
    ],
)
def test_add_splice_in_input_rejects_invalid_amounts(
    classified_utxos: list[ClassifiedUTXO],
    relative_amount: int,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        TxBuilder().add_splice_in_input(
            psbt=_build_splice_psbt(),
            coin=classified_utxos[2],
            relative_amount=relative_amount,
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
        )


def test_add_splice_in_input_rejects_duplicate_outpoint(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    coin = classified_utxos[2]
    tx_input = TxInput.from_hex(
        txid=coin.utxo.txid,
        vout=coin.utxo.vout,
        sequence=0xFFFFFFFE,
        value=coin.utxo.value,
        scriptpubkey=coin.utxo.scriptpubkey,
    )
    psbt = create_psbt(
        version=2,
        inputs=[tx_input],
        outputs=[
            TxOutput.from_address(
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                100_000,
            )
        ],
        locktime=0,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=coin.utxo.value,
                witness_utxo_script=tx_input.scriptpubkey,
                witness_script=b"",
            )
        ],
    )

    with pytest.raises(ValueError, match="already present"):
        TxBuilder().add_splice_in_input(
            psbt=psbt,
            coin=coin,
            relative_amount=40_000,
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
        )


def test_add_splice_in_input_rejects_already_signed_psbt(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    parsed = parse_psbt(_build_splice_psbt())
    parsed.input_maps[0].append(b"\x02" + b"\x02" + b"\x01" * 32, b"sig")

    with pytest.raises(ValueError, match="after input signing has started"):
        TxBuilder().add_splice_in_input(
            psbt=parsed.serialize(),
            coin=classified_utxos[2],
            relative_amount=40_000,
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
        )


def test_sign_splice_psbt_signs_only_jm_input(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()

    splice_psbt = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        relative_amount=40_000,
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )

    parsed = parse_psbt(splice_psbt)

    wallet.prepare_psbt_signing.return_value = SimpleNamespace(
        signable_count=1,
    )

    signed_result = SimpleNamespace(
        psbt=None,
        signed_indices=[1],
    )
    wallet.sign_psbt.return_value = signed_result

    private_key = PrivateKey(b"\x01" * 32)
    parsed = parse_psbt(splice_psbt)
    pubkey = wallet.get_key_for_address(
        classified_utxos[2].utxo.address,
    ).get_public_key_bytes(compressed=True)
    signature = sign_p2wpkh_input(
        parsed.transaction,
        1,
        create_p2wpkh_script_code(pubkey),
        classified_utxos[2].utxo.value,
        private_key,
    )
    parsed.append_input_key_value(
        1,
        b"\x02" + pubkey,
        signature,
    )
    signed_result.psbt = parsed.serialize()

    tx, txid, signed_psbt = builder.sign_splice_psbt(
        psbt=splice_psbt,
        signing_inputs={1: classified_utxos[2]},
        wallet=wallet,
    )

    signed = parse_psbt(signed_psbt)

    assert tx == parsed.transaction
    assert txid
    assert len(signed.transaction.inputs) == 2
    assert signed.input_maps[0].records == parsed.input_maps[0].records
    assert any(record.key[:1] == b"\x02" for record in signed.input_maps[1].records)


def test_sign_splice_psbt_rejects_signed_jm_input(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    parsed = parse_psbt(
        builder.add_splice_in_input(
            psbt=_build_splice_psbt(),
            coin=classified_utxos[2],
            relative_amount=40_000,
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )
    )
    pubkey = wallet.get_key_for_address.return_value.get_public_key_bytes(
        compressed=True,
    )
    parsed.input_maps[1].append(
        b"\x02" + pubkey,
        b"existing-signature",
    )

    with pytest.raises(
        RuntimeError,
        match="Splice signing input 1 already contains a signature",
    ):
        builder.sign_splice_psbt(
            psbt=parsed.serialize(),
            signing_inputs={1: classified_utxos[2]},
            wallet=wallet,
        )


def test_sign_splice_psbt_rejects_mismatched_jm_outpoint(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        relative_amount=40_000,
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )

    mismatched_coin = replace(
        classified_utxos[2],
        utxo=replace(
            classified_utxos[2].utxo,
            txid="bb" * 32,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="does not match the approved JoinMarket UTXO outpoint",
    ):
        builder.sign_splice_psbt(
            psbt=splice_psbt,
            signing_inputs={1: mismatched_coin},
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()


def test_sign_splice_psbt_rejects_mismatched_jm_value(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        relative_amount=40_000,
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )

    mismatched_coin = replace(
        classified_utxos[2],
        utxo=replace(
            classified_utxos[2].utxo,
            value=90_000,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="does not match the approved JoinMarket UTXO value or script",
    ):
        builder.sign_splice_psbt(
            psbt=splice_psbt,
            signing_inputs={1: mismatched_coin},
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()


def test_sign_splice_psbt_rejects_mismatched_jm_script(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        relative_amount=40_000,
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )

    mismatched_coin = replace(
        classified_utxos[2],
        utxo=replace(
            classified_utxos[2].utxo,
            scriptpubkey="0014" + "22" * 20,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="does not match the approved JoinMarket UTXO value or script",
    ):
        builder.sign_splice_psbt(
            psbt=splice_psbt,
            signing_inputs={1: mismatched_coin},
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()


def test_sign_splice_psbt_validates_every_jm_input(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        relative_amount=40_000,
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )

    mismatched_coin = replace(
        classified_utxos[2],
        utxo=replace(
            classified_utxos[2].utxo,
            txid="bb" * 32,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="does not match the approved JoinMarket UTXO outpoint",
    ):
        builder.sign_splice_psbt(
            psbt=splice_psbt,
            signing_inputs={
                0: mismatched_coin,
                1: classified_utxos[2],
            },
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()


def test_sign_splice_psbt_rejects_invalid_signing_index(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    with pytest.raises(
        RuntimeError,
        match="outside the PSBT",
    ):
        TxBuilder().sign_splice_psbt(
            psbt=_build_splice_psbt(),
            signing_inputs={1: classified_utxos[2]},
            wallet=_mock_wallet(),
        )
