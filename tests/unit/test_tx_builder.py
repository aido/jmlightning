from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from bitcointx.core.key import CKey
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
from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_UNSIGNED_TX,
    PSBT_GLOBAL_VERSION,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_FINAL_SCRIPTWITNESS,
    PSBT_IN_NON_WITNESS_UTXO,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_PROPRIETARY,
    PSBT_IN_SIGHASH_TYPE,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    ParsedPSBT,
    PSBTKeyValue,
    PSBTMap,
    parse_psbt,
)
from jmwallet.wallet.signing import sign_p2wpkh_input

from jmlightning.lightning.cln import (
    CLN_PSBT_SERIAL_ID_KEY,
    PSBT_GLOBAL_FALLBACK_LOCKTIME,
    PSBT_GLOBAL_INPUT_COUNT,
    PSBT_GLOBAL_OUTPUT_COUNT,
    PSBT_GLOBAL_TX_MODIFIABLE,
    PSBT_GLOBAL_TX_VERSION,
    PSBT_IN_OUTPUT_INDEX,
    PSBT_IN_PREVIOUS_TXID,
    PSBT_IN_SEQUENCE,
    PSBT_OUT_AMOUNT,
    PSBT_OUT_SCRIPT,
    _input_weight,
    new_serial_id,
    normalise_psbt_v2_to_v0,
)
from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan, FundingOutput, Planner
from jmlightning.tx_builder import SpliceContribution, TxBuilder


def _mock_wallet() -> Mock:
    wallet = Mock()

    private_key = CKey.from_secret_bytes(b"\x01" * 32)
    key = Mock()
    key.get_public_key_bytes.return_value = bytes(private_key.pub)

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
                    if record.key[:1] == bytes([PSBT_IN_BIP32_DERIVATION])
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


def _build_funding_plan(
    classified_utxos: list[ClassifiedUTXO],
) -> ExecutionPlan:
    planner = Planner()

    return planner.build_plan(
        selected_coins=classified_utxos[:2],
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )


def _build_cln_splice_psbt_v2() -> bytes:
    """Build a CLN-style BIP370 PSBT v2 with metadata to preserve."""

    def compact(value: int) -> bytes:
        if value < 0xFD:
            return bytes([value])
        raise AssertionError("fixture only needs one-byte CompactSize values")

    def record(key: bytes, value: bytes) -> bytes:
        return compact(len(key)) + key + compact(len(value)) + value

    def psbt_map(records: list[tuple[bytes, bytes]]) -> bytes:
        return b"".join(record(key, value) for key, value in records) + b"\x00"

    global_map = psbt_map(
        [
            (bytes([PSBT_GLOBAL_TX_VERSION]), (2).to_bytes(4, "little")),
            (bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]), (0).to_bytes(4, "little")),
            (bytes([PSBT_GLOBAL_INPUT_COUNT]), compact(1)),
            (bytes([PSBT_GLOBAL_OUTPUT_COUNT]), compact(1)),
            (bytes([PSBT_GLOBAL_TX_MODIFIABLE]), b"\x00"),
            (bytes([PSBT_IN_PROPRIETARY]) + b"cln-global", b"splice metadata"),
            (bytes([PSBT_GLOBAL_VERSION]), (2).to_bytes(4, "little")),
        ]
    )
    input_map = psbt_map(
        [
            (bytes([PSBT_IN_PREVIOUS_TXID]), bytes.fromhex("11" * 32)),
            (bytes([PSBT_IN_OUTPUT_INDEX]), (1).to_bytes(4, "little")),
            (bytes([PSBT_IN_SEQUENCE]), (0xFFFFFFFE).to_bytes(4, "little")),
            (
                bytes([PSBT_IN_WITNESS_UTXO]),
                (200_000).to_bytes(8, "little") + b"\x16\x00\x14" + b"\x22" * 20,
            ),
            (bytes([PSBT_IN_PROPRIETARY]) + b"cln-input", b"input metadata"),
        ]
    )
    output_map = psbt_map(
        [
            (bytes([PSBT_OUT_AMOUNT]), (199_847).to_bytes(8, "little")),
            (bytes([PSBT_OUT_SCRIPT]), b"\x00\x14" + b"\x33" * 20),
            (bytes([PSBT_IN_PROPRIETARY]) + b"cln-output", b"output metadata"),
        ]
    )
    return PSBT_MAGIC + global_map + input_map + output_map


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
    parsed.input_maps[0].append(
        bytes([PSBT_IN_PROPRIETARY]) + b"cln", b"input metadata"
    )
    parsed.output_maps[0].append(
        bytes([PSBT_IN_PROPRIETARY]) + b"cln", b"output metadata"
    )
    return parsed.serialize()


def _build_splice_plan(
    classified_utxos: list[ClassifiedUTXO],
    amount: int = 40_000,
    fee: int = 0,
    change: int = 60_000,
) -> ExecutionPlan:
    return ExecutionPlan(
        inputs=[classified_utxos[2]],
        amount=amount,
        fee=fee,
        vsize=0,
        change=change,
        warnings=[],
        rationale="test splice plan",
    )


def _sync_unsigned_tx(parsed: ParsedPSBT) -> None:
    unsigned_tx = serialize_transaction(
        parsed.transaction.version,
        parsed.transaction.inputs,
        parsed.transaction.outputs,
        parsed.transaction.locktime,
    )
    for index, record in enumerate(parsed.global_map.records):
        if record.key == bytes([PSBT_GLOBAL_UNSIGNED_TX]):
            parsed.global_map.records[index] = record.__class__(
                key=record.key,
                value=unsigned_tx,
            )
            return
    raise AssertionError("PSBT fixture is missing the unsigned transaction")


def test_cln_input_weight_rejects_unsupported_script() -> None:
    tx_input = TxInput.from_hex(
        txid="11" * 32,
        vout=0,
        sequence=0xFFFFFFFF,
        value=100_000,
        scriptpubkey="76a914" + "11" * 20 + "88ac",
    )
    psbt = create_psbt(
        version=2,
        inputs=[tx_input],
        outputs=[],
        locktime=0,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=100_000,
                witness_utxo_script=tx_input.scriptpubkey,
                witness_script=b"",
                sighash_type=1,
            )
        ],
    )
    parsed = parse_psbt(psbt)

    with pytest.raises(ValueError, match="Unsupported splice input script type"):
        _input_weight(parsed, 0)


def test_cln_input_weight_uses_non_witness_utxo() -> None:
    builder = TxBuilder()
    previous_output = TxOutput.from_address(
        "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        100_000,
    )
    previous_input = TxInput.from_hex(
        txid="aa" * 32,
        vout=0,
        sequence=0xFFFFFFFF,
        value=100_000,
        scriptpubkey=previous_output.script.hex(),
    )
    previous_tx = ParsedTransaction(
        version=2,
        inputs=[previous_input],
        outputs=[previous_output],
        witnesses=[],
        locktime=0,
        has_witness=False,
    )
    tx_input = TxInput.from_hex(
        txid=builder._txid(previous_tx),
        vout=0,
        sequence=0xFFFFFFFF,
        value=100_000,
        scriptpubkey=previous_output.script.hex(),
    )
    psbt = create_psbt(
        version=2,
        inputs=[tx_input],
        outputs=[],
        locktime=0,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=100_000,
                witness_utxo_script=previous_output.script,
                witness_script=b"",
            )
        ],
    )
    parsed = parse_psbt(psbt)
    parsed.input_maps[0].records = [
        record
        for record in parsed.input_maps[0].records
        if record.key != bytes([PSBT_IN_WITNESS_UTXO])
    ]
    parsed.input_maps[0].append(
        b"\x00",
        serialize_transaction(
            previous_tx.version,
            previous_tx.inputs,
            previous_tx.outputs,
            previous_tx.locktime,
        ),
    )

    assert _input_weight(parsed, 0) == 271


def test_cln_input_weight_rejects_non_witness_utxo_with_missing_output() -> None:
    builder = TxBuilder()
    previous_tx = ParsedTransaction(
        version=2,
        inputs=[],
        outputs=[],
        witnesses=[],
        locktime=0,
        has_witness=False,
    )
    tx_input = TxInput.from_hex(
        txid=builder._txid(previous_tx),
        vout=0,
        sequence=0xFFFFFFFF,
        value=100_000,
        scriptpubkey="0014" + "11" * 20,
    )
    psbt = create_psbt(
        version=2,
        inputs=[tx_input],
        outputs=[],
        locktime=0,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=100_000,
                witness_utxo_script=tx_input.scriptpubkey,
                witness_script=b"",
            )
        ],
    )
    parsed = parse_psbt(psbt)
    parsed.input_maps[0].records = [
        record
        for record in parsed.input_maps[0].records
        if record.key != bytes([PSBT_IN_WITNESS_UTXO])
    ]
    parsed.input_maps[0].append(
        b"\x00",
        serialize_transaction(
            previous_tx.version,
            previous_tx.inputs,
            previous_tx.outputs,
            previous_tx.locktime,
            previous_tx.witnesses,
        ),
    )

    with pytest.raises(ValueError, match="Invalid non-witness UTXO record"):
        _input_weight(parsed, 0)


def test_normalise_cln_psbt_v2_to_v0_preserves_metadata() -> None:
    normalised = normalise_psbt_v2_to_v0(_build_cln_splice_psbt_v2())
    parsed = parse_psbt(normalised)

    assert parsed.transaction.version == 2
    assert parsed.transaction.locktime == 0
    assert len(parsed.transaction.inputs) == 1
    assert parsed.transaction.inputs[0].txid == "11" * 32
    assert parsed.transaction.inputs[0].vout == 1
    assert parsed.transaction.inputs[0].sequence == 0xFFFFFFFE
    assert len(parsed.transaction.outputs) == 1
    assert parsed.transaction.outputs[0].value == 199_847
    assert parsed.transaction.outputs[0].script == b"\x00\x14" + b"\x33" * 20

    assert any(
        record.key == bytes([PSBT_IN_PROPRIETARY]) + b"cln-global"
        and record.value == b"splice metadata"
        for record in parsed.global_map.records
    )
    assert any(
        record.key == bytes([PSBT_IN_PROPRIETARY]) + b"cln-input"
        and record.value == b"input metadata"
        for record in parsed.input_maps[0].records
    )
    assert any(
        record.key == bytes([PSBT_IN_PROPRIETARY]) + b"cln-output"
        and record.value == b"output metadata"
        for record in parsed.output_maps[0].records
    )
    assert not any(
        record.key == bytes([PSBT_GLOBAL_TX_MODIFIABLE])
        for record in parsed.global_map.records
    )
    assert not any(
        record.key == bytes([PSBT_GLOBAL_VERSION])
        for record in parsed.global_map.records
    )


def test_normalise_psbt_v0_is_unchanged() -> None:
    psbt = _build_splice_psbt()

    assert normalise_psbt_v2_to_v0(psbt) == psbt


def test_estimate_splice_fee_matches_cln_weight_for_channel_psbt() -> None:
    builder = TxBuilder()

    fee, weight = builder.estimate_splice_fee(
        psbt=_build_cln_splice_psbt_v2(),
        feerate_per_kw=258,
    )

    # The fixture has a P2WPKH input/output. CLN charges 271 wu for that
    # input, plus 271 wu for the JM input, 124 wu for each output and 42 wu
    # for the common transaction fields.
    assert weight == 832
    assert fee == 214


def test_add_splice_in_input_accepts_cln_psbt_v2_and_preserves_metadata(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()

    result, _contribution = builder.add_splice_in_input(
        psbt=_build_cln_splice_psbt_v2(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
    )
    parsed = parse_psbt(result)

    assert len(parsed.transaction.inputs) == 2
    assert parsed.transaction.inputs[1].txid == classified_utxos[2].utxo.txid
    assert any(
        record.key == bytes([PSBT_IN_PROPRIETARY]) + b"cln-global"
        and record.value == b"splice metadata"
        for record in parsed.global_map.records
    )
    assert any(
        record.key == bytes([PSBT_IN_PROPRIETARY]) + b"cln-input"
        and record.value == b"input metadata"
        for record in parsed.input_maps[0].records
    )
    assert any(
        record.key == bytes([PSBT_IN_PROPRIETARY]) + b"cln-output"
        and record.value == b"output metadata"
        for record in parsed.output_maps[0].records
    )


def test_build_and_sign_multifunding_tx_builds_all_outputs_and_change(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    plan = ExecutionPlan(
        inputs=classified_utxos[:2],
        amount=150_000,
        fee=1_000,
        vsize=1_000,
        change=49_000,
        warnings=[],
        rationale="test multifunding plan",
        funding_outputs=[
            FundingOutput(amount=75_000, output_type="p2wsh"),
            FundingOutput(amount=75_000, output_type="p2wsh"),
        ],
    )

    tx, txid, psbt = builder.build_and_sign_multifunding_tx(
        plan=plan,
        funding_addresses=[
            "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        ],
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
    )

    assert len(tx.inputs) == 2
    assert [output.value for output in tx.outputs] == [75_000, 75_000, 49_000]
    assert txid
    assert psbt
    wallet.prepare_psbt_signing.assert_called_once()
    wallet.sign_psbt.assert_called_once()


def test_build_and_sign_multifunding_tx_rejects_address_count_mismatch(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    plan = ExecutionPlan(
        inputs=classified_utxos[:2],
        amount=150_000,
        fee=1_000,
        vsize=1_000,
        change=49_000,
        warnings=[],
        rationale="test multifunding plan",
        funding_outputs=[FundingOutput(amount=150_000, output_type="p2wsh")],
    )

    with pytest.raises(ValueError, match="output and address counts"):
        builder.build_and_sign_multifunding_tx(
            plan=plan,
            funding_addresses=[],
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()


def test_build_and_sign_multifunding_tx_rejects_wallet_key_script_mismatch(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    wallet.get_key_for_address.return_value.get_public_key_bytes.return_value = (
        b"\x02" * 33
    )
    plan = ExecutionPlan(
        inputs=classified_utxos[:2],
        amount=150_000,
        fee=1_000,
        vsize=1_000,
        change=49_000,
        warnings=[],
        rationale="test multifunding plan",
        funding_outputs=[
            FundingOutput(amount=75_000, output_type="p2wsh"),
            FundingOutput(amount=75_000, output_type="p2wsh"),
        ],
    )

    with pytest.raises(RuntimeError, match="does not match funding input script"):
        builder.build_and_sign_multifunding_tx(
            plan=plan,
            funding_addresses=[
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            ],
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=wallet,
        )

    wallet.prepare_psbt_signing.assert_not_called()


def test_build_and_sign_funding_tx_creates_funding_output(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
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
    parsed = parse_psbt(psbt)
    for input_map in parsed.input_maps:
        assert any(
            record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            for record in input_map.records
        )
        assert not any(
            record.key[:1] == bytes([PSBT_IN_FINAL_SCRIPTWITNESS])
            for record in input_map.records
        )


def test_build_and_sign_funding_tx_uses_psbt_signing(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
                if record.key == bytes([PSBT_IN_WITNESS_UTXO])
            )
        )
        witness[0] ^= 1
        parsed.input_maps[0].records = [
            replace(record, value=bytes(witness))
            if record.key == bytes([PSBT_IN_WITNESS_UTXO])
            else record
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
            CKey.from_secret_bytes(b"\x01" * 32),
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
    plan = _build_funding_plan(classified_utxos)
    wallet = _mock_wallet()

    def sign_psbt(signing_plan: SimpleNamespace) -> SimpleNamespace:
        parsed = parse_psbt(signing_plan.source_psbt)
        pubkey = wallet.get_key_for_address.return_value.get_public_key_bytes(
            compressed=True,
        )
        private_key = CKey.from_secret_bytes(b"\x01" * 32)
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
    plan = _build_funding_plan(classified_utxos)
    wallet = _mock_wallet()

    _, _, _, signed_psbt = builder.build_and_sign_funding_tx(
        plan=plan,
        funding_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        change_address=("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"),
        wallet=wallet,
    )

    assert signed_psbt.startswith(PSBT_MAGIC)
    parsed = parse_psbt(signed_psbt)
    for input_map in parsed.input_maps:
        assert any(
            record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            for record in input_map.records
        )
        assert not any(
            record.key[:1] == bytes([PSBT_IN_FINAL_SCRIPTWITNESS])
            for record in input_map.records
        )


def test_build_and_sign_funding_tx_returns_correct_txid(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
        finalise_transaction=True,
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

    # Both JoinMarket inputs must have a final witness and no partial signature.
    for index in signing_inputs:
        assert not any(
            record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            for record in parsed.input_maps[index].records
        )
        final_witness = next(
            record.value
            for record in parsed.input_maps[index].records
            if record.key[:1] == bytes([PSBT_IN_FINAL_SCRIPTWITNESS])
        )
        assert final_witness


def test_build_and_sign_funding_tx_rejects_duplicate_signed_indices(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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
    plan = _build_funding_plan(classified_utxos)
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

    plan = _build_funding_plan(classified_utxos)

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

    plan = _build_funding_plan(classified_utxos)

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
    plan = _build_funding_plan(classified_utxos)

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
    plan = _build_funding_plan(classified_utxos)

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
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    original = parse_psbt(_build_splice_psbt())

    updated, _contribution = builder.add_splice_in_input(
        psbt=original.serialize(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
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
        bytes([PSBT_IN_NON_WITNESS_UTXO]),
        bytes([PSBT_IN_WITNESS_UTXO]),
        bytes([PSBT_IN_SIGHASH_TYPE]),
        bytes([PSBT_IN_BIP32_DERIVATION]),
        bytes([PSBT_IN_PROPRIETARY]),
    ]
    assert len(parsed.output_maps[1].records) == 1
    assert parsed.output_maps[1].records[0].key == CLN_PSBT_SERIAL_ID_KEY

    assert parsed.unsigned_tx == serialize_transaction(
        parsed.transaction.version,
        parsed.transaction.inputs,
        parsed.transaction.outputs,
        parsed.transaction.locktime,
    )


@pytest.mark.parametrize(
    ("amount", "change", "error"),
    [
        (40_000, 59_999, "Transaction plan has inconsistent amounts"),
        (100_001, -1, "Transaction change cannot be negative."),
    ],
)
def test_add_splice_in_input_rejects_invalid_amounts(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
    amount: int,
    change: int,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        TxBuilder().add_splice_in_input(
            psbt=_build_splice_psbt(),
            coin=classified_utxos[2],
            plan=_build_splice_plan(
                classified_utxos,
                amount=amount,
                change=change,
            ),
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
            prev_tx=splice_prev_tx,
        )


def test_add_splice_in_input_rejects_duplicate_outpoint(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
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
            plan=_build_splice_plan(classified_utxos),
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
            prev_tx=splice_prev_tx,
        )


def test_add_splice_in_input_rejects_already_signed_psbt(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    parsed = parse_psbt(_build_splice_psbt())
    parsed.input_maps[0].append(b"\x02" + b"\x02" + b"\x01" * 32, b"sig")

    with pytest.raises(ValueError, match="after input signing has started"):
        TxBuilder().add_splice_in_input(
            psbt=parsed.serialize(),
            coin=classified_utxos[2],
            plan=_build_splice_plan(classified_utxos),
            change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            wallet=_mock_wallet(),
            prev_tx=splice_prev_tx,
        )


def test_find_splice_input_index_finds_appended_jm_input(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
    )

    assert builder.find_splice_input_index(splice_psbt, classified_utxos[2]) == 1


def test_find_splice_input_index_rejects_missing_jm_input(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()

    with pytest.raises(
        RuntimeError,
        match="does not contain the approved JoinMarket UTXO",
    ):
        builder.find_splice_input_index(_build_splice_psbt(), classified_utxos[2])


def test_find_splice_input_index_rejects_duplicate_jm_input(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
    )
    parsed = parse_psbt(splice_psbt)
    parsed.transaction.inputs.append(parsed.transaction.inputs[1])
    parsed.transaction.witnesses.append([])
    parsed.input_maps.append(parsed.input_maps[1])

    with pytest.raises(
        RuntimeError,
        match="Invalid splice PSBT",
    ):
        builder.find_splice_input_index(parsed.serialize(), classified_utxos[2])


def test_sign_splice_psbt_signs_only_jm_input(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()

    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
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

    private_key = CKey.from_secret_bytes(b"\x01" * 32)
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
    assert any(
        record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
        for record in signed.input_maps[1].records
    )


def test_sign_splice_psbt_rejects_signed_jm_input(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
    )
    parsed = parse_psbt(splice_psbt)
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
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
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
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
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
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
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
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    wallet = _mock_wallet()
    splice_psbt, _contribution = builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=wallet,
        prev_tx=splice_prev_tx,
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


def _capture_splice_contribution(
    builder: TxBuilder,
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> tuple[bytes, SpliceContribution]:
    return builder.add_splice_in_input(
        psbt=_build_splice_psbt(),
        coin=classified_utxos[2],
        plan=_build_splice_plan(classified_utxos),
        change_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        wallet=_mock_wallet(),
        prev_tx=splice_prev_tx,
    )


@pytest.mark.parametrize("mutation", ["remove_change", "extra_output"])
def test_validate_splice_psbt_rejects_output_tampering(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
    mutation: str,
) -> None:
    builder = TxBuilder()
    splice_psbt, contribution = _capture_splice_contribution(
        builder, classified_utxos, splice_prev_tx
    )
    parsed = parse_psbt(splice_psbt)

    if mutation == "remove_change":
        parsed.transaction.outputs.pop()
        parsed.output_maps.pop()
    else:
        parsed.transaction.outputs.append(
            TxOutput.from_address(
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                1_000,
            )
        )
        parsed.output_maps.append(parsed.output_maps[-1].__class__())
    _sync_unsigned_tx(parsed)

    with pytest.raises(RuntimeError, match="permitted output changes"):
        builder.validate_splice_psbt(parsed.serialize(), contribution)


def test_validate_splice_psbt_rejects_fee_inflation(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    splice_psbt, contribution = _capture_splice_contribution(
        builder, classified_utxos, splice_prev_tx
    )
    parsed = parse_psbt(splice_psbt)

    parsed.transaction.outputs[0] = TxOutput(
        value=contribution.channel_output[0] + contribution.channel_contribution,
        script=contribution.channel_output[1],
    )
    parsed.transaction.inputs.append(
        TxInput.from_hex(
            txid="cc" * 32,
            vout=0,
            sequence=0xFFFFFFFF,
            value=1_000,
            scriptpubkey="0014" + "44" * 20,
        )
    )
    parsed.transaction.witnesses.append([])
    parsed.input_maps.append(
        PSBTMap(
            records=[
                PSBTKeyValue(
                    key=bytes([PSBT_IN_WITNESS_UTXO]),
                    value=(1_000).to_bytes(8, "little")
                    + b"\x16\x00\x14"
                    + b"\x44" * 20,
                )
            ]
        )
    )
    _sync_unsigned_tx(parsed)

    with pytest.raises(RuntimeError, match="exceeds maximum permitted fee"):
        builder.validate_splice_psbt(parsed.serialize(), contribution)


def test_validate_splice_psbt_accepts_final_channel_contribution(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    splice_psbt, contribution = _capture_splice_contribution(
        builder, classified_utxos, splice_prev_tx
    )
    parsed = parse_psbt(splice_psbt)

    parsed.transaction.outputs[0] = TxOutput(
        value=contribution.channel_output[0] + contribution.channel_contribution,
        script=contribution.channel_output[1],
    )
    _sync_unsigned_tx(parsed)

    builder.validate_splice_psbt(parsed.serialize(), contribution)


def test_validate_splice_psbt_accepts_reordered_inputs_and_outputs(
    classified_utxos: list[ClassifiedUTXO],
    splice_prev_tx: bytes,
) -> None:
    builder = TxBuilder()
    splice_psbt, contribution = _capture_splice_contribution(
        builder, classified_utxos, splice_prev_tx
    )
    parsed = parse_psbt(splice_psbt)

    parsed.transaction.inputs.reverse()
    parsed.transaction.witnesses.reverse()
    parsed.input_maps.reverse()
    parsed.transaction.outputs.reverse()
    parsed.output_maps.reverse()
    _sync_unsigned_tx(parsed)

    builder.validate_splice_psbt(parsed.serialize(), contribution)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: replace(plan, inputs=[]), "at least one input"),
        (lambda plan: replace(plan, amount=-1), "amount must be positive"),
        (lambda plan: replace(plan, fee=-1), "fee cannot be negative"),
        (lambda plan: replace(plan, change=-1), "change cannot be negative"),
    ],
)
def test_validate_plan_rejects_invalid_financial_invariants(
    classified_utxos: list[ClassifiedUTXO],
    mutate: Callable[[ExecutionPlan], ExecutionPlan],
    message: str,
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
    invalid = mutate(plan)

    with pytest.raises(ValueError, match=message):
        builder._validate_plan(invalid)


def test_validate_plan_rejects_duplicate_inputs(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
    duplicate = replace(plan, inputs=[plan.inputs[0], plan.inputs[0]])

    with pytest.raises(ValueError, match="duplicate inputs"):
        builder._validate_plan(duplicate)


def test_validate_plan_rejects_inconsistent_amounts(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    builder = TxBuilder()
    plan = _build_funding_plan(classified_utxos)
    inconsistent = replace(plan, change=plan.change + 1)

    with pytest.raises(ValueError, match="inconsistent amounts"):
        builder._validate_plan(inconsistent)


def test_new_cln_serial_id_is_even_and_unique() -> None:
    serial_id = new_serial_id({2, 4, 6})

    assert serial_id % 2 == 0
    assert serial_id not in {2, 4, 6}
