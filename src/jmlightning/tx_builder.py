from collections.abc import Mapping

from jmcore.bitcoin import (
    BIP32Derivation,
    ParsedTransaction,
    PSBTInput,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    create_psbt,
    encode_varint,
    hash256,
    parse_derivation_path,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmwallet.wallet.psbt import (
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_FINAL_SCRIPTSIG,
    PSBT_IN_FINAL_SCRIPTWITNESS,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_SIGHASH_TYPE,
    PSBT_IN_WITNESS_UTXO,
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
    def _remove_empty_witness_scripts(psbt: bytes) -> bytes:
        """Remove empty witness-script records emitted by jmcore 0.37.0.

        jmcore 0.37.0 unconditionally serialises ``PSBT_IN_WITNESS_SCRIPT``
        even when the witness script is empty. An empty value is not a valid
        witness script record for a P2WPKH input and Core Lightning rejects the
        resulting PSBT. Keep this compatibility workaround local to the PSBT
        boundary rather than changing the transaction model or inserting a
        fabricated script. This is a workaround for a jmcore 0.37.0 bug.

        Remove this workaround when the minimum supported jmcore version no
        longer emits empty witness-script records.
        """
        magic = b"psbt\xff"
        if not psbt.startswith(magic):
            raise ValueError("invalid PSBT magic")

        def _read_compact_size(data: bytes, offset: int) -> tuple[int, int]:
            if offset >= len(data):
                raise ValueError("truncated PSBT compact size")
            first = data[offset]
            offset += 1
            if first < 0xFD:
                return first, offset
            if first == 0xFD:
                size = 2
            elif first == 0xFE:
                size = 4
            else:
                size = 8
            end = offset + size
            if end > len(data):
                raise ValueError("truncated PSBT compact size")
            return int.from_bytes(data[offset:end], "little"), end

        def _write_compact_size(value: int) -> bytes:
            if value < 0:
                raise ValueError("negative PSBT compact size")
            if value < 0xFD:
                return bytes([value])
            if value <= 0xFFFF:
                return b"\xfd" + value.to_bytes(2, "little")
            if value <= 0xFFFFFFFF:
                return b"\xfe" + value.to_bytes(4, "little")
            return b"\xff" + value.to_bytes(8, "little")

        offset = len(magic)
        output = bytearray(magic)

        while offset < len(psbt):
            key_len, offset = _read_compact_size(psbt, offset)
            if key_len == 0:
                output.append(0)
                continue

            key_end = offset + key_len
            if key_end > len(psbt):
                raise ValueError("truncated PSBT key")
            key = psbt[offset:key_end]
            offset = key_end

            value_len, offset = _read_compact_size(psbt, offset)
            value_end = offset + value_len
            if value_end > len(psbt):
                raise ValueError("truncated PSBT value")
            value = psbt[offset:value_end]
            offset = value_end

            if key == b"\x05" and not value:
                continue

            output.extend(_write_compact_size(len(key)))
            output.extend(key)
            output.extend(_write_compact_size(len(value)))
            output.extend(value)

        return bytes(output)

    def build_and_sign_funding_tx(
        self,
        plan: ExecutionPlan,
        funding_address: str,
        change_address: str,
        wallet: WalletService,
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
        )

        return tx, txid, funding_vout, signed_psbt

    def add_splice_in_input(
        self,
        psbt: bytes,
        coin: ClassifiedUTXO,
        relative_amount: int,
        change_address: str,
        wallet: WalletService,
    ) -> bytes:
        """Add one JoinMarket input and matching change to a splice PSBT.

        The transaction supplied by Core Lightning remains authoritative.
        This method only appends the approved JoinMarket input, adds any
        resulting change output, and updates the BIP174 unsigned-transaction
        record. Existing PSBT map records are retained unchanged.

        The transaction must not already contain signatures: changing its
        inputs or outputs would invalidate them.
        """
        if relative_amount <= 0:
            raise ValueError("Splice-in amount must be positive")

        try:
            parsed = parse_psbt(psbt)
        except PSBTError as exc:
            raise ValueError("Invalid splice PSBT") from exc

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

        if coin.utxo.value < relative_amount:
            raise ValueError(
                "Splice input value is smaller than the requested splice-in amount"
            )

        key = wallet.get_key_for_address(coin.utxo.address)
        if key is None:
            raise RuntimeError(
                f"Unable to resolve wallet key for {coin.utxo.txid}:{coin.utxo.vout}"
            )

        expected_pubkey = key.get_public_key_bytes(compressed=True)
        expected_script = pubkey_to_p2wpkh_script(expected_pubkey)
        actual_script = bytes.fromhex(coin.utxo.scriptpubkey)
        if actual_script != expected_script:
            raise RuntimeError(
                "JoinMarket wallet key does not match splice input script "
                f"for input {len(parsed.transaction.inputs)}"
            )

        change = coin.utxo.value - relative_amount
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

        witness_utxo = (
            coin.utxo.value.to_bytes(8, "little", signed=False)
            + encode_varint(len(actual_script))
            + actual_script
        )
        new_input_map = PSBTMap()
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
        parsed.input_maps.append(new_input_map)
        if change_output is not None:
            parsed.output_maps.append(PSBTMap())

        unsigned_tx = serialize_transaction(
            parsed.transaction.version,
            parsed.transaction.inputs,
            parsed.transaction.outputs,
            parsed.transaction.locktime,
        )
        for index, record in enumerate(parsed.global_map.records):
            if record.key == b"\x00":
                parsed.global_map.records[index] = PSBTKeyValue(
                    key=record.key,
                    value=unsigned_tx,
                )
                break
        else:
            raise RuntimeError("Splice PSBT is missing the unsigned transaction")

        return parsed.serialize()

    @staticmethod
    def _validate_splice_signing_input(
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

        return self._sign_psbt(
            unsigned_psbt=psbt,
            tx=parsed_psbt.transaction,
            signing_inputs=signing_inputs,
            wallet=wallet,
        )

    def _build_and_sign_tx(
        self,
        tx: ParsedTransaction,
        signing_inputs: Mapping[int, ClassifiedUTXO],
        psbt_inputs: list[PSBTInput],
        wallet: WalletService,
    ) -> tuple[ParsedTransaction, str, bytes]:
        """Build and cryptographically validate a signed transaction PSBT."""

        unsigned_psbt = create_psbt(
            version=tx.version,
            inputs=tx.inputs,
            outputs=tx.outputs,
            locktime=tx.locktime,
            psbt_inputs=psbt_inputs,
        )
        unsigned_psbt = self._remove_empty_witness_scripts(unsigned_psbt)

        return self._sign_psbt(
            unsigned_psbt=unsigned_psbt,
            tx=tx,
            signing_inputs=signing_inputs,
            wallet=wallet,
        )

    def _sign_psbt(
        self,
        unsigned_psbt: bytes,
        tx: ParsedTransaction,
        signing_inputs: Mapping[int, ClassifiedUTXO],
        wallet: WalletService,
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

            if not verify_p2wpkh_signature(
                tx,
                index,
                create_p2wpkh_script_code(expected_pubkey),
                coin.utxo.value,
                signatures[0].value,
                expected_pubkey,
            ):
                raise RuntimeError(
                    "JoinMarket wallet returned an invalid P2WPKH signature "
                    f"for input {index}"
                )

        txid = self._txid(tx)

        return tx, txid, signed_psbt
