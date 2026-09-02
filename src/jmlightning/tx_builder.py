from jmcore.bitcoin import (
    BIP32Derivation,
    ParsedTransaction,
    PSBTInput,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    create_psbt,
    hash256,
    parse_derivation_path,
    serialize_transaction,
)
from jmwallet.wallet.psbt import PSBT_IN_PARTIAL_SIG, PSBTError, parse_psbt
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import verify_p2wpkh_signature

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
            raise ValueError("Funding transaction requires at least one input.")

        if plan.amount <= 0:
            raise ValueError("Funding transaction amount must be positive.")

        if plan.fee < 0:
            raise ValueError("Transaction fee cannot be negative.")

        if plan.change < 0:
            raise ValueError("Transaction change cannot be negative.")

        outpoints = [(coin.utxo.txid, coin.utxo.vout) for coin in plan.inputs]

        if len(outpoints) != len(set(outpoints)):
            raise ValueError("Funding transaction contains duplicate inputs.")

        input_total = sum(coin.utxo.value for coin in plan.inputs)

        expected_total = plan.amount + plan.fee + plan.change

        if input_total != expected_total:
            raise ValueError(
                "Funding transaction plan has inconsistent amounts: "
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
        witness script record for a P2WPKH input, and Core Lightning rejects the
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
                            pubkey=key.get_public_key_bytes(
                                compressed=True,
                            ),
                            fingerprint=wallet.master_key.fingerprint,
                            path=parse_derivation_path(
                                coin.utxo.path,
                            ),
                        )
                    ],
                )
            )

        unsigned_psbt = create_psbt(
            version=tx.version,
            inputs=tx.inputs,
            outputs=tx.outputs,
            locktime=tx.locktime,
            psbt_inputs=psbt_inputs,
        )
        unsigned_psbt = self._remove_empty_witness_scripts(unsigned_psbt)

        signing_plan = wallet.prepare_psbt_signing(
            unsigned_psbt,
            scan_range=0,
        )

        if signing_plan.signable_count != len(plan.inputs):
            raise RuntimeError(
                "JoinMarket wallet did not identify all funding inputs "
                "as signable wallet inputs"
            )

        signed_result = wallet.sign_psbt(signing_plan)
        signed_psbt = signed_result.psbt

        signed_indices = list(signed_result.signed_indices)
        expected_indices = set(range(len(plan.inputs)))

        if len(signed_indices) != len(expected_indices):
            raise RuntimeError("JoinMarket wallet did not sign all funding inputs")

        if len(set(signed_indices)) != len(signed_indices):
            raise RuntimeError(
                "JoinMarket wallet returned duplicate signed input indices"
            )

        if set(signed_indices) != expected_indices:
            raise RuntimeError(
                "JoinMarket wallet did not sign exactly the funding inputs"
            )

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

        for index, (source_map, signed_map, coin) in enumerate(
            zip(
                source_parsed_psbt.input_maps,
                signed_parsed_psbt.input_maps,
                plan.inputs,
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

        return tx, txid, funding_vout, signed_psbt
