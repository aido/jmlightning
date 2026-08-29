from jmcore.bitcoin import (
    BIP32Derivation,
    ParsedTransaction,
    PSBTInput,
    TxInput,
    TxOutput,
    create_psbt,
    hash256,
    parse_derivation_path,
    serialize_transaction,
)
from jmwallet.wallet.psbt import PSBTError, parse_psbt
from jmwallet.wallet.service import WalletService

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

        txid = self._txid(tx)

        return tx, txid, funding_vout, signed_psbt
