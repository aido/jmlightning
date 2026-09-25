import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace

from jmcore.bitcoin import (
    BIP32Derivation,
    ParsedTransaction,
    PSBTInput,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    create_psbt,
    decode_varint,
    encode_varint,
    hash256,
    parse_derivation_path,
    parse_transaction_bytes,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_UNSIGNED_TX,
    PSBT_GLOBAL_VERSION,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_FINAL_SCRIPTSIG,
    PSBT_IN_FINAL_SCRIPTWITNESS,
    PSBT_IN_NON_WITNESS_UTXO,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_PROPRIETARY,
    PSBT_IN_SIGHASH_TYPE,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    ParsedPSBT,
    PSBTError,
    PSBTKeyValue,
    PSBTMap,
    parse_psbt,
)
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import verify_p2wpkh_signature

from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan

# BIP370 PSBT v2 global fields. jmwallet only supports the
# BIP174/v0 global fields, so the v2 transaction-structure fields are
# removed when converting a CLN splice PSBT to v0.
PSBT_GLOBAL_TX_VERSION = 0x02
PSBT_GLOBAL_FALLBACK_LOCKTIME = 0x03
PSBT_GLOBAL_INPUT_COUNT = 0x04
PSBT_GLOBAL_OUTPUT_COUNT = 0x05
PSBT_GLOBAL_TX_MODIFIABLE = 0x06
PSBT_IN_PREVIOUS_TXID = 0x0E
PSBT_IN_OUTPUT_INDEX = 0x0F
PSBT_IN_SEQUENCE = 0x10
PSBT_OUT_AMOUNT = 0x03
PSBT_OUT_SCRIPT = 0x04

# Core Lightning proprietary PSBT key for interactive transaction serial IDs.
# Key: proprietary type (0xfc), prefix length (9), "lightning", subtype 1.
CLN_PSBT_SERIAL_ID_KEY = bytes([PSBT_IN_PROPRIETARY]) + b"\x09lightning\x01"


@dataclass(frozen=True)
class SpliceContribution:
    jm_outpoint: tuple[str, int]
    jm_value: int
    change_script: bytes | None
    change_value: int
    channel_output: tuple[int, bytes]
    channel_contribution: int
    baseline_outputs: tuple[tuple[int, bytes], ...]
    max_fee: int


class TxBuilder:
    def __init__(self) -> None:
        pass

    def _txid(self, tx: ParsedTransaction) -> str:
        raw = serialize_transaction(
            tx.version,
            tx.inputs,
            tx.outputs,
            tx.locktime,
        )
        return hash256(raw)[::-1].hex()

    def _validate_plan(self, plan: ExecutionPlan) -> None:
        """Validate the financial and input invariants of an execution plan."""
        if not plan.inputs:
            raise ValueError("Transaction requires at least one input.")

        if plan.amount <= 0:
            raise ValueError("Transaction amount must be positive.")

        if plan.fee < 0:
            raise ValueError("Transaction fee cannot be negative.")

        if plan.change < 0:
            raise ValueError("Transaction change cannot be negative.")

        outpoints = [(coin.utxo.txid, coin.utxo.vout) for coin in plan.inputs]

        if len(outpoints) != len(set(outpoints)):
            raise ValueError("Transaction contains duplicate inputs.")

        input_total = sum(coin.utxo.value for coin in plan.inputs)

        expected_total = plan.amount + plan.fee + plan.change

        if input_total != expected_total:
            raise ValueError(
                "Transaction plan has inconsistent amounts: "
                f"inputs={input_total}, "
                f"amount={plan.amount}, "
                f"fee={plan.fee}, "
                f"change={plan.change}"
            )

    @staticmethod
    def _normalise_psbt_v2_to_v0(psbt: bytes) -> bytes:
        """Convert a BIP370 PSBT v2 to the BIP174 v0 form used by jmwallet.

        Core Lightning emits splice PSBTs using PSBT v2. jmwallet's PSBT
        parser deliberately accepts v0 only, so normalise the transaction
        structure at the PSBT boundary while retaining every non-structural
        record verbatim.
        """
        magic = PSBT_MAGIC
        if not psbt.startswith(magic):
            raise ValueError("invalid PSBT magic")

        def _read_compact_size(data: bytes, offset: int) -> tuple[int, int]:
            if offset >= len(data):
                raise ValueError("truncated PSBT compact size")
            first = data[offset]
            offset += 1
            if first < 0xFD:
                return first, offset
            size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
            end = offset + size
            if end > len(data):
                raise ValueError("truncated PSBT compact size")
            value = int.from_bytes(data[offset:end], "little")
            minimum = {0xFD: 0xFD, 0xFE: 0x10000, 0xFF: 0x100000000}[first]
            if value < minimum:
                raise ValueError("noncanonical PSBT compact size")
            return value, end

        def _read_map(
            data: bytes, offset: int
        ) -> tuple[list[tuple[bytes, bytes]], int]:
            records: list[tuple[bytes, bytes]] = []
            while True:
                key_len, offset = _read_compact_size(data, offset)
                if key_len == 0:
                    return records, offset
                key_end = offset + key_len
                if key_end > len(data):
                    raise ValueError("truncated PSBT key")
                key = data[offset:key_end]
                offset = key_end
                value_len, offset = _read_compact_size(data, offset)
                value_end = offset + value_len
                if value_end > len(data):
                    raise ValueError("truncated PSBT value")
                records.append((key, data[offset:value_end]))
                offset = value_end

        def _write_map(records: list[tuple[bytes, bytes]]) -> bytes:
            result = bytearray()
            for key, value in records:
                result.extend(encode_varint(len(key)))
                result.extend(key)
                result.extend(encode_varint(len(value)))
                result.extend(value)
            result.append(0)
            return bytes(result)

        offset = len(magic)
        global_records, offset = _read_map(psbt, offset)

        version_records = [
            value
            for key, value in global_records
            if key == bytes([PSBT_GLOBAL_VERSION])
        ]
        if not version_records:
            return psbt
        if len(version_records) != 1 or len(version_records[0]) != 4:
            raise ValueError("invalid PSBT version record")

        version = int.from_bytes(version_records[0], "little")
        if version == 0:
            return psbt
        if version != 2:
            raise ValueError(f"unsupported PSBT version: {version}")

        input_count_records = [
            value
            for key, value in global_records
            if key == bytes([PSBT_GLOBAL_INPUT_COUNT])
        ]
        output_count_records = [
            value
            for key, value in global_records
            if key == bytes([PSBT_GLOBAL_OUTPUT_COUNT])
        ]
        tx_version_records = [
            value
            for key, value in global_records
            if key == bytes([PSBT_GLOBAL_TX_VERSION])
        ]
        locktime_records = [
            value
            for key, value in global_records
            if key == bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME])
        ]

        if len(input_count_records) != 1 or len(output_count_records) != 1:
            raise ValueError("PSBT v2 is missing input/output counts")
        if len(tx_version_records) != 1 or len(tx_version_records[0]) != 4:
            raise ValueError("PSBT v2 has an invalid transaction version")
        if len(locktime_records) > 1 or (
            locktime_records and len(locktime_records[0]) != 4
        ):
            raise ValueError("PSBT v2 has an invalid fallback locktime")

        def _decode_count(value: bytes, context: str) -> int:
            if not value:
                raise ValueError(f"PSBT v2 {context} count is empty")
            count, end = _read_compact_size(value, 0)
            if end != len(value):
                raise ValueError(f"PSBT v2 {context} count has trailing data")
            return count

        input_count = _decode_count(input_count_records[0], "input")
        output_count = _decode_count(output_count_records[0], "output")

        input_maps: list[list[tuple[bytes, bytes]]] = []
        for _ in range(input_count):
            records, offset = _read_map(psbt, offset)
            input_maps.append(records)

        output_maps: list[list[tuple[bytes, bytes]]] = []
        for _ in range(output_count):
            records, offset = _read_map(psbt, offset)
            output_maps.append(records)

        if offset != len(psbt):
            raise ValueError("trailing data after PSBT maps")

        def _singleton(
            records: list[tuple[bytes, bytes]], key_type: bytes, context: str
        ) -> bytes:
            values = [value for key, value in records if key == key_type]
            if len(values) != 1:
                raise ValueError(f"PSBT v2 {context} record must occur exactly once")
            return values[0]

        tx = bytearray()
        tx.extend(tx_version_records[0])
        tx.extend(encode_varint(input_count))
        for index, records in enumerate(input_maps):
            txid = _singleton(
                records, bytes([PSBT_IN_PREVIOUS_TXID]), f"input {index} previous txid"
            )
            vout = _singleton(
                records, bytes([PSBT_IN_OUTPUT_INDEX]), f"input {index} output index"
            )
            sequence_values = [
                value for key, value in records if key == bytes([PSBT_IN_SEQUENCE])
            ]
            if len(txid) != 32 or len(vout) != 4:
                raise ValueError(f"PSBT v2 input {index} has invalid outpoint")
            if len(sequence_values) > 1 or (
                sequence_values and len(sequence_values[0]) != 4
            ):
                raise ValueError(f"PSBT v2 input {index} has invalid sequence")
            tx.extend(txid)
            tx.extend(vout)
            tx.append(0)
            tx.extend(sequence_values[0] if sequence_values else b"\xff\xff\xff\xff")

        tx.extend(encode_varint(output_count))
        for index, records in enumerate(output_maps):
            amount = _singleton(
                records, bytes([PSBT_OUT_AMOUNT]), f"output {index} amount"
            )
            script = _singleton(
                records, bytes([PSBT_OUT_SCRIPT]), f"output {index} script"
            )
            if len(amount) != 8:
                raise ValueError(f"PSBT v2 output {index} has invalid amount")
            tx.extend(amount)
            tx.extend(encode_varint(len(script)))
            tx.extend(script)

        tx.extend(locktime_records[0] if locktime_records else b"\x00\x00\x00\x00")

        # Keep all metadata and remove only v2 structural globals. The v0
        # unsigned transaction replaces the v2 transaction fields.
        v2_globals_to_remove = {
            bytes([PSBT_GLOBAL_UNSIGNED_TX]),
            bytes([PSBT_GLOBAL_TX_VERSION]),
            bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]),
            bytes([PSBT_GLOBAL_INPUT_COUNT]),
            bytes([PSBT_GLOBAL_OUTPUT_COUNT]),
            bytes([PSBT_GLOBAL_TX_MODIFIABLE]),
            bytes([PSBT_GLOBAL_VERSION]),
        }
        v0_globals = [
            (key, value)
            for key, value in global_records
            if key not in v2_globals_to_remove
        ]
        v0_globals.insert(0, (bytes([PSBT_GLOBAL_UNSIGNED_TX]), bytes(tx)))

        result = bytearray(magic)
        result.extend(_write_map(v0_globals))
        for records in input_maps:
            result.extend(
                _write_map(
                    [
                        (key, value)
                        for key, value in records
                        if key
                        not in {
                            bytes([PSBT_IN_PREVIOUS_TXID]),
                            bytes([PSBT_IN_OUTPUT_INDEX]),
                            bytes([PSBT_IN_SEQUENCE]),
                        }
                    ]
                )
            )
        for records in output_maps:
            result.extend(
                _write_map(
                    [
                        (key, value)
                        for key, value in records
                        if key
                        not in {bytes([PSBT_OUT_AMOUNT]), bytes([PSBT_OUT_SCRIPT])}
                    ]
                )
            )

        return bytes(result)

    def build_and_sign_funding_tx(
        self,
        plan: ExecutionPlan,
        funding_address: str,
        change_address: str,
        wallet: WalletService,
        finalise_psbt: bool = False,
    ) -> tuple[ParsedTransaction, str, int, bytes]:

        self._validate_plan(plan)

        tx_inputs: list[TxInput] = []

        for coin in plan.inputs:
            tx_inputs.append(
                TxInput.from_hex(
                    txid=coin.utxo.txid,
                    vout=coin.utxo.vout,
                    sequence=0xFFFFFFFF,
                    value=coin.utxo.value,
                    scriptpubkey=coin.utxo.scriptpubkey,
                )
            )

        tx_outputs = [
            TxOutput.from_address(
                funding_address,
                plan.amount,
            )
        ]

        funding_vout = 0

        if plan.change > 0:
            tx_outputs.append(
                TxOutput.from_address(
                    change_address,
                    plan.change,
                )
            )

        tx = ParsedTransaction(
            version=2,
            inputs=tx_inputs,
            outputs=tx_outputs,
            witnesses=[[] for _ in tx_inputs],
            locktime=0,
            has_witness=True,
        )

        # Build the BIP174 PSBT describing the exact unsigned funding
        # transaction. The BIP32 origins allow jmwallet to identify
        # the wallet-owned inputs when performing PSBT signing.
        psbt_inputs: list[PSBTInput] = []

        for coin in plan.inputs:
            key = wallet.get_key_for_address(coin.utxo.address)

            if key is None:
                raise RuntimeError(
                    "Unable to resolve wallet key for "
                    f"{coin.utxo.txid}:{coin.utxo.vout}"
                )

            expected_pubkey = key.get_public_key_bytes(compressed=True)
            expected_script = pubkey_to_p2wpkh_script(expected_pubkey)
            actual_script = bytes.fromhex(coin.utxo.scriptpubkey)
            if actual_script != expected_script:
                raise RuntimeError(
                    "JoinMarket wallet key does not match funding input script "
                    f"for input {len(psbt_inputs)}"
                )

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
                            path=parse_derivation_path(
                                coin.utxo.path,
                            ),
                        )
                    ],
                )
            )

        signing_inputs = {index: coin for index, coin in enumerate(plan.inputs)}

        tx, txid, signed_psbt = self._build_and_sign_tx(
            tx=tx,
            signing_inputs=signing_inputs,
            psbt_inputs=psbt_inputs,
            wallet=wallet,
            finalise_transaction=finalise_psbt,
        )

        return tx, txid, funding_vout, signed_psbt

    def build_and_sign_multifunding_tx(
        self,
        plan: ExecutionPlan,
        funding_addresses: list[str],
        change_address: str,
        wallet: WalletService,
        finalise_psbt: bool = False,
    ) -> tuple[ParsedTransaction, str, bytes]:
        """Build and sign one transaction funding multiple channels."""
        self._validate_plan(plan)

        if len(plan.funding_outputs) != len(funding_addresses):
            raise ValueError("Funding output and address counts must match")

        if not plan.funding_outputs:
            raise ValueError("Multifunding plan requires funding outputs")

        if sum(output.amount for output in plan.funding_outputs) != plan.amount:
            raise ValueError("Multifunding plan has inconsistent funding amounts")

        tx_inputs = [
            TxInput.from_hex(
                txid=coin.utxo.txid,
                vout=coin.utxo.vout,
                sequence=0xFFFFFFFF,
                value=coin.utxo.value,
                scriptpubkey=coin.utxo.scriptpubkey,
            )
            for coin in plan.inputs
        ]

        tx_outputs = [
            TxOutput.from_address(address, output.amount)
            for address, output in zip(
                funding_addresses,
                plan.funding_outputs,
                strict=True,
            )
        ]

        if plan.change > 0:
            tx_outputs.append(TxOutput.from_address(change_address, plan.change))

        tx = ParsedTransaction(
            version=2,
            inputs=tx_inputs,
            outputs=tx_outputs,
            witnesses=[[] for _ in tx_inputs],
            locktime=0,
            has_witness=True,
        )

        psbt_inputs: list[PSBTInput] = []

        for coin in plan.inputs:
            key = wallet.get_key_for_address(coin.utxo.address)

            if key is None:
                raise RuntimeError(
                    "Unable to resolve wallet key for "
                    f"{coin.utxo.txid}:{coin.utxo.vout}"
                )

            expected_pubkey = key.get_public_key_bytes(compressed=True)
            expected_script = pubkey_to_p2wpkh_script(expected_pubkey)
            actual_script = bytes.fromhex(coin.utxo.scriptpubkey)
            if actual_script != expected_script:
                raise RuntimeError(
                    "JoinMarket wallet key does not match funding input script "
                    f"for input {len(psbt_inputs)}"
                )

            psbt_inputs.append(
                PSBTInput(
                    witness_utxo_value=coin.utxo.value,
                    witness_utxo_script=actual_script,
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

        signing_inputs = {index: coin for index, coin in enumerate(plan.inputs)}

        return self._build_and_sign_tx(
            tx=tx,
            signing_inputs=signing_inputs,
            psbt_inputs=psbt_inputs,
            wallet=wallet,
            finalise_transaction=finalise_psbt,
        )

    @staticmethod
    def _new_cln_serial_id(existing: set[int]) -> int:
        """Generate a fresh initiator-role serial ID for a splice PSBT."""
        while True:
            # CLN encodes the transaction role in the low bit. jm-lightning
            # is the splice initiator when it adds its own input/output, so
            # these serial IDs must have even parity.
            serial_id = secrets.randbits(63) << 1
            if serial_id != 0 and serial_id not in existing:
                return serial_id

    @staticmethod
    def _cln_input_weight(parsed_psbt: ParsedPSBT, index: int) -> int:
        """Calculate CLN's splice weight for one input.

        This mirrors ``psbt_input_get_weight(..., PSBT_GUESS_2OF2)`` in
        Core Lightning for standard SegWit inputs.
        """
        input_map = parsed_psbt.input_maps[index]
        witness_records = [
            record
            for record in input_map.records
            if record.key[:1] == bytes([PSBT_IN_WITNESS_UTXO])
        ]

        script: bytes | None = None
        if len(witness_records) == 1:
            value = witness_records[0].value
            if len(value) < 9:
                raise ValueError("Invalid witness UTXO record")
            script_len, offset = decode_varint(value, 8)
            if offset + script_len != len(value):
                raise ValueError("Invalid witness UTXO record")
            script = value[offset:]

        if script is None:
            non_witness_records = [
                record
                for record in input_map.records
                if record.key[:1] == bytes([PSBT_IN_NON_WITNESS_UTXO])
            ]
            if len(non_witness_records) == 1:
                previous_transaction = parse_transaction_bytes(
                    non_witness_records[0].value,
                )
                vout = parsed_psbt.transaction.inputs[index].vout
                if vout >= len(previous_transaction.outputs):
                    raise ValueError("Invalid non-witness UTXO record")
                script = previous_transaction.outputs[vout].script

        if script is None:
            raise ValueError("Splice input is missing UTXO data")

        # These values mirror CLN's bitcoin_tx_input_weight() and
        # bitcoin_tx_input_witness_weight()/bitcoin_tx_2of2_input_witness_weight().
        if script.startswith(b"\x00\x14"):
            return 271  # P2WPKH
        if script.startswith(b"\x00\x20"):
            return 387  # P2WSH, guessed as the channel's 2-of-2 input
        if script.startswith(b"\x51\x20"):
            return 230  # P2TR

        raise ValueError("Unsupported splice input script type")

    @staticmethod
    def _cln_output_weight(script: bytes) -> int:
        """Calculate CLN's weight for one standard transaction output."""
        return (8 + len(encode_varint(len(script))) + len(script)) * 4

    @staticmethod
    def _cln_core_weight(num_inputs: int, num_outputs: int) -> int:
        """Calculate CLN's common transaction-field weight."""
        return (
            4 + len(encode_varint(num_inputs)) + len(encode_varint(num_outputs)) + 4
        ) * 4 + 2

    def estimate_splice_fee(
        self,
        psbt: bytes,
        feerate_per_kw: int,
        add_change_output: bool = True,
    ) -> tuple[int, int]:
        """Estimate the initiator fee using CLN's splice weight calculation.

        ``feerate_per_kw`` uses CLN's native satoshis per 1000 weight
        units. The returned tuple is ``(fee, weight)``.
        """
        if feerate_per_kw <= 0:
            raise ValueError("Fee rate must be positive")

        try:
            parsed_psbt = parse_psbt(self._normalise_psbt_v2_to_v0(psbt))
        except PSBTError as exc:
            raise ValueError("Invalid splice PSBT") from exc

        input_weight = sum(
            self._cln_input_weight(parsed_psbt, index)
            for index in range(len(parsed_psbt.input_maps))
        )
        output_weight = sum(
            self._cln_output_weight(output.script)
            for output in parsed_psbt.transaction.outputs
        )

        # jm-lightning adds one P2WPKH JoinMarket input and, unless the
        # splice is a sweep, one P2WPKH change output.
        input_weight += 271
        output_count = len(parsed_psbt.transaction.outputs)
        if add_change_output:
            output_weight += 124
            output_count += 1

        weight = (
            input_weight
            + output_weight
            + self._cln_core_weight(
                len(parsed_psbt.transaction.inputs) + 1,
                output_count,
            )
        )

        fee = (feerate_per_kw * weight) // 1000
        return fee, weight

    @staticmethod
    def _cln_serial_ids(parsed_psbt: ParsedPSBT) -> set[int]:
        """Return all existing Core Lightning serial IDs in a PSBT."""
        serial_ids: set[int] = set()
        for psbt_map in [*parsed_psbt.input_maps, *parsed_psbt.output_maps]:
            for record in psbt_map.records:
                if record.key != CLN_PSBT_SERIAL_ID_KEY:
                    continue
                if len(record.value) != 8:
                    raise ValueError("Invalid Core Lightning serial ID")
                serial_ids.add(int.from_bytes(record.value, "big"))
        return serial_ids

    def add_splice_in_input(
        self,
        psbt: bytes,
        coin: ClassifiedUTXO,
        plan: ExecutionPlan,
        change_address: str,
        wallet: WalletService,
        prev_tx: bytes,
    ) -> tuple[bytes, SpliceContribution]:
        """Add one JoinMarket input and matching change to a splice PSBT.

        The transaction supplied by Core Lightning remains authoritative.
        This method applies the already-approved JoinMarket execution plan:
        it appends the planned input, adds the planned change output and
        updates the BIP174 unsigned-transaction record. Existing PSBT map
        records are retained unchanged.

        Core Lightning's interactive transaction protocol requires every
        input and output to carry a proprietary serial ID and every input to
        carry its full previous transaction. These records are therefore
        added here for the JoinMarket input and change output.

        The transaction must not already contain signatures: changing its
        inputs or outputs would invalidate them.
        """
        self._validate_plan(plan)

        if len(plan.inputs) != 1:
            raise ValueError("Splice-in plan must contain exactly one input")

        if plan.inputs[0] is not coin:
            raise ValueError("Splice-in plan does not match the selected input")

        try:
            psbt = self._normalise_psbt_v2_to_v0(psbt)
            parsed = parse_psbt(psbt)
            previous_transaction = parse_transaction_bytes(prev_tx)
        except (PSBTError, ValueError) as exc:
            raise ValueError("Invalid splice PSBT or previous transaction") from exc

        if self._txid(previous_transaction) != coin.utxo.txid:
            raise ValueError(
                "Splice input previous transaction does not match the approved "
                f"UTXO {coin.utxo.txid}:{coin.utxo.vout}"
            )

        if coin.utxo.vout >= len(previous_transaction.outputs):
            raise ValueError(
                "Splice input previous transaction does not contain the approved "
                f"output {coin.utxo.vout}"
            )

        previous_output = previous_transaction.outputs[coin.utxo.vout]
        actual_script = bytes.fromhex(coin.utxo.scriptpubkey)
        if (
            previous_output.value != coin.utxo.value
            or previous_output.script != actual_script
        ):
            raise ValueError(
                "Splice input previous transaction output does not match the "
                "approved JoinMarket UTXO"
            )

        for input_index, input_map in enumerate(parsed.input_maps):
            if any(
                record.key[:1]
                in {
                    bytes([PSBT_IN_PARTIAL_SIG]),
                    bytes([PSBT_IN_FINAL_SCRIPTSIG]),
                    bytes([PSBT_IN_FINAL_SCRIPTWITNESS]),
                }
                for record in input_map.records
            ):
                raise ValueError(
                    "Cannot modify a splice PSBT after input signing has started "
                    f"(input {input_index})"
                )

        existing_outpoints = {
            (tx_input.txid, tx_input.vout) for tx_input in parsed.transaction.inputs
        }
        coin_outpoint = (coin.utxo.txid, coin.utxo.vout)
        if coin_outpoint in existing_outpoints:
            raise ValueError(
                f"Splice input is already present: {coin.utxo.txid}:{coin.utxo.vout}"
            )

        if coin.utxo.value < plan.amount + plan.fee:
            raise ValueError(
                "Splice input value is smaller than the planned "
                "splice-in amount and fee"
            )

        key = wallet.get_key_for_address(coin.utxo.address)
        if key is None:
            raise RuntimeError(
                f"Unable to resolve wallet key for {coin.utxo.txid}:{coin.utxo.vout}"
            )

        expected_pubkey = key.get_public_key_bytes(compressed=True)
        expected_script = pubkey_to_p2wpkh_script(expected_pubkey)
        if actual_script != expected_script:
            raise RuntimeError(
                "JoinMarket wallet key does not match splice input script "
                f"for input {len(parsed.transaction.inputs)}"
            )

        expected_change = coin.utxo.value - plan.amount - plan.fee
        if expected_change != plan.change:
            raise ValueError("Splice-in plan change does not match selected input")

        baseline_outputs = tuple(
            (output.value, output.script) for output in parsed.transaction.outputs
        )
        channel_candidates = [
            output
            for output in parsed.transaction.outputs
            if output.script.startswith(b"\x00\x20")
        ]
        if len(channel_candidates) == 1:
            channel_output = (
                channel_candidates[0].value,
                channel_candidates[0].script,
            )
        elif len(parsed.transaction.outputs) == 1:
            channel_output = baseline_outputs[0]
        else:
            raise ValueError(
                "Splice PSBT must identify exactly one channel funding output "
                "before the JoinMarket input is added"
            )

        change = plan.change
        change_output = (
            TxOutput.from_address(change_address, change) if change > 0 else None
        )

        new_input = TxInput.from_hex(
            txid=coin.utxo.txid,
            vout=coin.utxo.vout,
            sequence=0xFFFFFFFF,
            value=coin.utxo.value,
            scriptpubkey=coin.utxo.scriptpubkey,
        )
        parsed.transaction.inputs.append(new_input)
        parsed.transaction.witnesses.append([])

        if change_output is not None:
            parsed.transaction.outputs.append(change_output)

        serial_ids = self._cln_serial_ids(parsed)
        jm_input_serial_id = self._new_cln_serial_id(serial_ids)
        serial_ids.add(jm_input_serial_id)
        change_serial_id = (
            self._new_cln_serial_id(serial_ids) if change_output is not None else None
        )

        witness_utxo = (
            coin.utxo.value.to_bytes(8, "little", signed=False)
            + encode_varint(len(actual_script))
            + actual_script
        )
        new_input_map = PSBTMap()
        new_input_map.append(
            bytes([PSBT_IN_NON_WITNESS_UTXO]),
            prev_tx,
        )
        new_input_map.append(
            bytes([PSBT_IN_WITNESS_UTXO]),
            witness_utxo,
        )
        new_input_map.append(
            bytes([PSBT_IN_SIGHASH_TYPE]),
            (1).to_bytes(4, "little"),
        )
        new_input_map.append(
            bytes([PSBT_IN_BIP32_DERIVATION]) + expected_pubkey,
            wallet.master_key.fingerprint
            + b"".join(
                path_index.to_bytes(4, "little", signed=False)
                for path_index in parse_derivation_path(coin.utxo.path)
            ),
        )
        new_input_map.append(
            CLN_PSBT_SERIAL_ID_KEY,
            jm_input_serial_id.to_bytes(8, "big"),
        )
        parsed.input_maps.append(new_input_map)
        if change_output is not None:
            assert change_serial_id is not None
            change_map = PSBTMap()
            change_map.append(
                CLN_PSBT_SERIAL_ID_KEY,
                change_serial_id.to_bytes(8, "big"),
            )
            parsed.output_maps.append(change_map)

        unsigned_tx = serialize_transaction(
            parsed.transaction.version,
            parsed.transaction.inputs,
            parsed.transaction.outputs,
            parsed.transaction.locktime,
        )
        for index, record in enumerate(parsed.global_map.records):
            if record.key == bytes([PSBT_GLOBAL_UNSIGNED_TX]):
                parsed.global_map.records[index] = PSBTKeyValue(
                    key=record.key,
                    value=unsigned_tx,
                )
                break
        else:
            raise RuntimeError("Splice PSBT is missing the unsigned transaction")

        contribution = SpliceContribution(
            jm_outpoint=coin_outpoint,
            jm_value=coin.utxo.value,
            change_script=(change_output.script if change_output is not None else None),
            change_value=change,
            channel_output=channel_output,
            channel_contribution=plan.amount,
            baseline_outputs=baseline_outputs,
            max_fee=plan.fee,
        )
        return parsed.serialize(), contribution

    @staticmethod
    def _psbt_input_value(parsed_psbt: ParsedPSBT, index: int) -> int:
        records = parsed_psbt.input_maps[index].records
        witness = [r for r in records if r.key[:1] == bytes([PSBT_IN_WITNESS_UTXO])]
        if len(witness) == 1:
            if len(witness[0].value) < 8:
                raise RuntimeError("Splice PSBT contains an invalid witness UTXO")
            return int.from_bytes(witness[0].value[:8], "little")

        non_witness = [
            r for r in records if r.key[:1] == bytes([PSBT_IN_NON_WITNESS_UTXO])
        ]
        if len(non_witness) != 1:
            raise RuntimeError(
                f"Splice PSBT input {index} has no authoritative UTXO value"
            )
        try:
            previous = parse_transaction_bytes(non_witness[0].value)
        except ValueError as exc:
            raise RuntimeError(
                "Splice PSBT contains an invalid non-witness UTXO"
            ) from exc
        tx_input = parsed_psbt.transaction.inputs[index]
        if tx_input.vout >= len(previous.outputs):
            raise RuntimeError("Splice PSBT input references a missing UTXO")
        return previous.outputs[tx_input.vout].value

    def validate_splice_psbt(
        self,
        psbt: bytes,
        contribution: SpliceContribution,
    ) -> None:
        """Validate immutable splice economics before JoinMarket signing."""
        try:
            parsed = parse_psbt(self._normalise_psbt_v2_to_v0(psbt))
        except (PSBTError, ValueError) as exc:
            raise RuntimeError("Invalid splice PSBT") from exc

        if len(parsed.input_maps) != len(parsed.transaction.inputs):
            raise RuntimeError("Splice PSBT input map count does not match transaction")
        if len(parsed.output_maps) != len(parsed.transaction.outputs):
            raise RuntimeError(
                "Splice PSBT output map count does not match transaction"
            )

        matches = [
            i
            for i, tx_input in enumerate(parsed.transaction.inputs)
            if (tx_input.txid, tx_input.vout) == contribution.jm_outpoint
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Splice PSBT must contain the approved JoinMarket UTXO exactly once"
            )
        if self._psbt_input_value(parsed, matches[0]) != contribution.jm_value:
            raise RuntimeError("Splice PSBT JoinMarket input value was changed")

        actual = Counter((o.value, o.script) for o in parsed.transaction.outputs)
        expected_pre = Counter(contribution.baseline_outputs)
        if contribution.change_script is not None:
            expected_pre[(contribution.change_value, contribution.change_script)] += 1

        expected_final = Counter(contribution.baseline_outputs)
        expected_final[contribution.channel_output] -= 1
        if expected_final[contribution.channel_output] == 0:
            del expected_final[contribution.channel_output]
        channel_after = (
            contribution.channel_output[0] + contribution.channel_contribution,
            contribution.channel_output[1],
        )
        expected_final[channel_after] += 1
        if contribution.change_script is not None:
            expected_final[(contribution.change_value, contribution.change_script)] += 1

        pre_contribution = actual == expected_pre
        final_contribution = actual == expected_final
        if not (pre_contribution or final_contribution):
            raise RuntimeError(
                "Splice PSBT does not preserve the intended channel contribution "
                "and permitted output changes"
            )

        # The pre-contribution PSBT is an intermediate protocol state. The
        # JoinMarket input has been added but CLN has not necessarily applied
        # the corresponding channel-output increase yet, so its apparent
        # fee is not meaningful. Enforce the fee ceiling once the final
        # channel contribution is present, immediately before signing.
        if final_contribution:
            input_total = sum(
                self._psbt_input_value(parsed, index)
                for index in range(len(parsed.transaction.inputs))
            )
            output_total = sum(output.value for output in parsed.transaction.outputs)
            fee = input_total - output_total
            if fee < 0:
                raise RuntimeError("Splice PSBT has negative fee")
            if fee > contribution.max_fee:
                raise RuntimeError(
                    "Splice PSBT fee exceeds maximum permitted fee "
                    f"({fee} > {contribution.max_fee})"
                )

    def _validate_splice_signing_input(
        self,
        parsed_psbt: ParsedPSBT,
        index: int,
        coin: ClassifiedUTXO,
    ) -> None:
        """Verify that a splice signing input is the approved JoinMarket UTXO."""
        transaction_input = parsed_psbt.transaction.inputs[index]

        if (
            transaction_input.txid != coin.utxo.txid
            or transaction_input.vout != coin.utxo.vout
        ):
            raise RuntimeError(
                f"Splice signing input {index} does not match the approved "
                "JoinMarket UTXO outpoint"
            )

        witness_records = [
            record
            for record in parsed_psbt.input_maps[index].records
            if record.key[:1] == bytes([PSBT_IN_WITNESS_UTXO])
        ]
        if len(witness_records) != 1:
            raise RuntimeError(
                f"Splice signing input {index} must contain exactly one "
                "witness UTXO record"
            )

        expected_script = bytes.fromhex(coin.utxo.scriptpubkey)
        expected_witness_utxo = (
            coin.utxo.value.to_bytes(8, "little", signed=False)
            + encode_varint(len(expected_script))
            + expected_script
        )
        if witness_records[0].value != expected_witness_utxo:
            raise RuntimeError(
                f"Splice signing input {index} does not match the approved "
                "JoinMarket UTXO value or script"
            )

        non_witness_records = [
            record
            for record in parsed_psbt.input_maps[index].records
            if record.key[:1] == bytes([PSBT_IN_NON_WITNESS_UTXO])
        ]
        if len(non_witness_records) != 1:
            raise RuntimeError(
                f"Splice signing input {index} must contain exactly one "
                "non-witness UTXO record"
            )

        try:
            previous_transaction = parse_transaction_bytes(
                non_witness_records[0].value,
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Splice signing input {index} has an invalid non-witness UTXO"
            ) from exc

        if self._txid(previous_transaction) != coin.utxo.txid:
            raise RuntimeError(
                f"Splice signing input {index} non-witness UTXO does not "
                "match the approved JoinMarket UTXO outpoint"
            )

        if coin.utxo.vout >= len(previous_transaction.outputs):
            raise RuntimeError(
                f"Splice signing input {index} non-witness UTXO does not "
                f"contain output {coin.utxo.vout}"
            )

        previous_output = previous_transaction.outputs[coin.utxo.vout]
        if (
            previous_output.value != coin.utxo.value
            or previous_output.script != expected_script
        ):
            raise RuntimeError(
                f"Splice signing input {index} non-witness UTXO does not "
                "match the approved JoinMarket UTXO value or script"
            )

    def find_splice_input_index(
        self,
        psbt: bytes,
        coin: ClassifiedUTXO,
    ) -> int:
        """Find the approved JoinMarket input in a negotiated splice PSBT."""
        try:
            parsed_psbt = parse_psbt(self._normalise_psbt_v2_to_v0(psbt))
        except PSBTError as exc:
            raise RuntimeError("Invalid splice PSBT") from exc

        matches = [
            index
            for index, transaction_input in enumerate(parsed_psbt.transaction.inputs)
            if (transaction_input.txid, transaction_input.vout)
            == (coin.utxo.txid, coin.utxo.vout)
        ]

        if not matches:
            raise RuntimeError(
                "Negotiated splice PSBT does not contain the approved JoinMarket UTXO"
            )

        if len(matches) != 1:
            raise RuntimeError(
                "Negotiated splice PSBT contains the approved "
                "JoinMarket UTXO more than once"
            )

        return matches[0]

    def sign_splice_psbt(
        self,
        psbt: bytes,
        signing_inputs: Mapping[int, ClassifiedUTXO],
        wallet: WalletService,
    ) -> tuple[ParsedTransaction, str, bytes]:
        """Sign JM-owned inputs in an existing splice PSBT.

        Core Lightning owns the splice transaction and its PSBT metadata.
        Unlike the normal funding path, this method never reconstructs the
        PSBT. The supplied PSBT is passed directly to jmwallet so CLN/peer
        metadata and signatures on non-JM inputs are preserved.
        """
        try:
            psbt = self._normalise_psbt_v2_to_v0(psbt)
            parsed_psbt = parse_psbt(psbt)
        except PSBTError as exc:
            raise RuntimeError("Invalid splice PSBT") from exc

        if len(parsed_psbt.input_maps) != len(parsed_psbt.transaction.inputs):
            raise RuntimeError("Splice PSBT input map count does not match transaction")

        if len(parsed_psbt.output_maps) != len(parsed_psbt.transaction.outputs):
            raise RuntimeError(
                "Splice PSBT output map count does not match transaction"
            )

        for index in signing_inputs:
            if index < 0 or index >= len(parsed_psbt.input_maps):
                raise RuntimeError(
                    f"Splice signing input index {index} is outside the PSBT"
                )

            if any(
                record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
                for record in parsed_psbt.input_maps[index].records
            ):
                raise RuntimeError(
                    f"Splice signing input {index} already contains a signature"
                )

            self._validate_splice_signing_input(
                parsed_psbt, index, signing_inputs[index]
            )

        # jmwallet's PSBT signer validates every P2WSH input as a JoinMarket
        # fidelity bond. A splice PSBT also contains CLN's channel funding
        # input, which is a P2WSH 2-of-2 output and is not a JoinMarket input.
        #
        # Sign against a temporary PSBT where non-JM P2WSH witness_utxos are
        # represented as unowned P2WPKH outputs. The BIP143 sighash for the JM
        # P2WPKH input does not commit to the other inputs' prevout scripts or
        # amounts. The original PSBT is retained and only the returned JM
        # partial signatures are merged back into it.
        signing_psbt = self._sanitise_splice_signing_psbt(
            parsed_psbt,
            signing_inputs,
        )

        _signed_tx, txid, signed_signing_psbt = self._sign_psbt(
            unsigned_psbt=signing_psbt,
            tx=parsed_psbt.transaction,
            signing_inputs=signing_inputs,
            wallet=wallet,
        )

        try:
            signed_parsed_psbt = parse_psbt(signed_signing_psbt)
        except PSBTError as exc:
            raise RuntimeError(
                "JoinMarket wallet returned an invalid signed PSBT"
            ) from exc

        for index in signing_inputs:
            signatures = [
                record
                for record in signed_parsed_psbt.input_maps[index].records
                if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            ]
            if len(signatures) != 1:
                raise RuntimeError(
                    "JoinMarket wallet did not return exactly one signature "
                    f"for input {index}"
                )
            parsed_psbt.input_maps[index].append(
                signatures[0].key,
                signatures[0].value,
            )

        signed_psbt = parsed_psbt.serialize()
        return parsed_psbt.transaction, txid, signed_psbt

    def _sanitise_splice_signing_psbt(
        self,
        parsed_psbt: ParsedPSBT,
        signing_inputs: Mapping[int, ClassifiedUTXO],
    ) -> bytes:
        """Build the restricted PSBT presented to jmwallet for signing.

        jmwallet validates every P2WSH input as a fidelity bond during
        PSBT review. A CLN splice necessarily contains the channel's P2WSH
        funding input, so that input must not be interpreted as a JoinMarket
        fidelity bond. Only the approved JoinMarket inputs are allowed to
        retain their real UTXO metadata in the signing PSBT.
        """
        signing_psbt = parse_psbt(parsed_psbt.serialize())

        dummy_script = b"\x00\x14" + b"\x00" * 20
        for index, input_map in enumerate(signing_psbt.input_maps):
            if index in signing_inputs:
                continue

            for record_index, record in enumerate(input_map.records):
                if record.key[:1] != bytes([PSBT_IN_WITNESS_UTXO]):
                    continue

                if len(record.value) < 9:
                    raise RuntimeError(
                        f"Splice signing input {index} has an invalid witness UTXO"
                    )
                script_length, offset = decode_varint(record.value, 8)
                if offset + script_length != len(record.value):
                    raise RuntimeError(
                        f"Splice signing input {index} has an invalid witness UTXO"
                    )

                script = record.value[offset:]
                if not script.startswith(b"\x00\x20"):
                    break

                value = record.value[:8]
                input_map.records[record_index] = replace(
                    record,
                    value=value + bytes([len(dummy_script)]) + dummy_script,
                )
                break

        return signing_psbt.serialize()

    def _build_and_sign_tx(
        self,
        tx: ParsedTransaction,
        signing_inputs: Mapping[int, ClassifiedUTXO],
        psbt_inputs: list[PSBTInput],
        wallet: WalletService,
        finalise_transaction: bool,
    ) -> tuple[ParsedTransaction, str, bytes]:
        """Build and cryptographically validate a signed transaction PSBT."""

        unsigned_psbt = create_psbt(
            version=tx.version,
            inputs=tx.inputs,
            outputs=tx.outputs,
            locktime=tx.locktime,
            psbt_inputs=psbt_inputs,
        )
        signed_tx, txid, signed_psbt = self._sign_psbt(
            unsigned_psbt=unsigned_psbt,
            tx=tx,
            signing_inputs=signing_inputs,
            wallet=wallet,
            finalise_transaction=finalise_transaction,
        )
        return signed_tx, txid, signed_psbt

    def _sign_psbt(
        self,
        unsigned_psbt: bytes,
        tx: ParsedTransaction,
        signing_inputs: Mapping[int, ClassifiedUTXO],
        wallet: WalletService,
        finalise_transaction: bool = False,
    ) -> tuple[ParsedTransaction, str, bytes]:
        """Sign selected inputs in an existing PSBT and validate the result."""

        signing_plan = wallet.prepare_psbt_signing(
            unsigned_psbt,
            scan_range=0,
        )

        if signing_plan.signable_count != len(signing_inputs):
            raise RuntimeError(
                "JoinMarket wallet did not identify all inputs "
                "as signable wallet inputs"
            )

        signed_result = wallet.sign_psbt(signing_plan)
        signed_psbt = signed_result.psbt

        signed_indices = list(signed_result.signed_indices)
        if any(
            isinstance(index, bool) or not isinstance(index, int)
            for index in signed_indices
        ):
            raise RuntimeError(
                "JoinMarket wallet returned invalid signed input indices"
            )
        expected_indices = set(signing_inputs)

        if len(signed_indices) != len(expected_indices):
            raise RuntimeError("JoinMarket wallet did not sign all inputs")

        if len(set(signed_indices)) != len(signed_indices):
            raise RuntimeError(
                "JoinMarket wallet returned duplicate signed input indices"
            )

        if set(signed_indices) != expected_indices:
            raise RuntimeError("JoinMarket wallet did not sign exactly the inputs")

        try:
            signed_parsed_psbt = parse_psbt(signed_psbt)
        except PSBTError as exc:
            raise RuntimeError(
                "JoinMarket wallet returned an invalid signed PSBT"
            ) from exc

        expected_unsigned_tx = serialize_transaction(
            tx.version,
            tx.inputs,
            tx.outputs,
            tx.locktime,
        )

        if signed_parsed_psbt.unsigned_tx != expected_unsigned_tx:
            raise RuntimeError(
                "JoinMarket wallet returned a PSBT for a different transaction"
            )

        # sign_psbt() is allowed to add partial signatures, but no other
        # PSBT data may change after the transaction was reviewed. This keeps
        # the wallet signing boundary tied to the exact inputs and outputs
        # that were presented to it.
        source_parsed_psbt = parse_psbt(unsigned_psbt)
        if (
            signed_parsed_psbt.global_map.records
            != source_parsed_psbt.global_map.records
        ):
            raise RuntimeError(
                "JoinMarket wallet changed PSBT global metadata while signing"
            )
        if signed_parsed_psbt.output_maps != source_parsed_psbt.output_maps:
            raise RuntimeError(
                "JoinMarket wallet changed PSBT output metadata while signing"
            )

        for index, (source_map, signed_map) in enumerate(
            zip(
                source_parsed_psbt.input_maps,
                signed_parsed_psbt.input_maps,
                strict=True,
            )
        ):
            source_non_signature = [
                record
                for record in source_map.records
                if record.key[:1] != bytes([PSBT_IN_PARTIAL_SIG])
            ]
            signed_non_signature = [
                record
                for record in signed_map.records
                if record.key[:1] != bytes([PSBT_IN_PARTIAL_SIG])
            ]
            if signed_non_signature != source_non_signature:
                raise RuntimeError(
                    f"JoinMarket wallet changed PSBT input metadata for input {index}"
                )

            source_signatures = [
                record
                for record in source_map.records
                if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            ]
            signed_signatures = [
                record
                for record in signed_map.records
                if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            ]

            if index not in signing_inputs:
                if signed_signatures != source_signatures:
                    raise RuntimeError(
                        "JoinMarket wallet changed PSBT signatures for "
                        f"non-JM input {index}"
                    )
                continue

            coin = signing_inputs[index]
            key = wallet.get_key_for_address(coin.utxo.address)
            if key is None:
                raise RuntimeError(
                    "Unable to resolve wallet key for "
                    f"{coin.utxo.txid}:{coin.utxo.vout}"
                )
            expected_pubkey = key.get_public_key_bytes(compressed=True)
            signature_key = bytes([PSBT_IN_PARTIAL_SIG]) + expected_pubkey
            signatures = [
                record
                for record in signed_map.records
                if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            ]
            if len(signatures) != 1 or signatures[0].key != signature_key:
                raise RuntimeError(
                    "JoinMarket wallet did not return exactly one signature "
                    f"for input {index}"
                )
            if not signatures[0].value:
                raise RuntimeError(
                    f"JoinMarket wallet returned an empty signature for input {index}"
                )
            if signatures[0].value[-1] != 1:
                raise RuntimeError(
                    "JoinMarket wallet returned an unsupported sighash type "
                    f"for input {index}"
                )

            signature = signatures[0].value
            if not verify_p2wpkh_signature(
                tx,
                index,
                create_p2wpkh_script_code(expected_pubkey),
                coin.utxo.value,
                signature,
                expected_pubkey,
            ):
                raise RuntimeError(
                    "JoinMarket wallet returned an invalid P2WPKH signature "
                    f"for input {index}"
                )

            if finalise_transaction:
                # The PSBT signer returns a partial signature. Build the
                # corresponding P2WPKH witness for callers that broadcast
                # the returned transaction directly.
                if len(tx.witnesses) < len(tx.inputs):
                    tx.witnesses.extend(
                        [[] for _ in range(len(tx.inputs) - len(tx.witnesses))]
                    )
                tx.witnesses[index] = [signature, expected_pubkey]

        if finalise_transaction:
            self._finalize_psbt_inputs(
                signed_parsed_psbt,
                signing_inputs,
                tx,
            )
            signed_psbt = signed_parsed_psbt.serialize()

        txid = self._txid(tx)

        return tx, txid, signed_psbt

    @staticmethod
    def _finalize_psbt_inputs(
        parsed_psbt: ParsedPSBT,
        signing_inputs: Mapping[int, ClassifiedUTXO],
        tx: ParsedTransaction,
    ) -> None:
        """Replace JM partial signatures with final P2WPKH witnesses."""
        for index in signing_inputs:
            input_map = parsed_psbt.input_maps[index]
            signatures = [
                record
                for record in input_map.records
                if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
            ]
            if len(signatures) != 1:
                raise RuntimeError(
                    "JoinMarket wallet did not return exactly one signature "
                    f"for input {index}"
                )

            signature = signatures[0].value
            pubkey = signatures[0].key[1:]
            witness = encode_varint(2) + encode_varint(len(signature)) + signature
            witness += encode_varint(len(pubkey)) + pubkey

            input_map.records = [
                record
                for record in input_map.records
                if record.key[:1] != bytes([PSBT_IN_PARTIAL_SIG])
            ]
            input_map.append(bytes([PSBT_IN_FINAL_SCRIPTWITNESS]), witness)

            if len(tx.witnesses) < len(tx.inputs):
                tx.witnesses.extend(
                    [[] for _ in range(len(tx.inputs) - len(tx.witnesses))]
                )
            tx.witnesses[index] = [signature, pubkey]
