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

    def build_and_sign_funding_tx(
        self,
        plan: ExecutionPlan,
        funding_address: str,
        change_address: str,
        wallet: WalletService,
    ) -> tuple[ParsedTransaction, str, int, bytes]:
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

        if len(signed_result.signed_indices) != len(plan.inputs):
            raise RuntimeError("JoinMarket wallet did not sign all funding inputs")

        txid = self._txid(tx)

        return tx, txid, funding_vout, signed_psbt
