import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    hash256,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmwallet.wallet.models import AddressStatus, UTXOInfo

from jmlightning.models import ClassifiedUTXO
from jmlightning.policy import PolicyEngine


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def classified_utxos() -> list[ClassifiedUTXO]:
    scriptpubkey = pubkey_to_p2wpkh_script(CKey(b"\x01" * 32).pub).hex()

    def make_utxo(
        txid: str,
        vout: int,
        status: AddressStatus,
    ) -> ClassifiedUTXO:
        utxo = UTXOInfo(
            txid=txid,
            vout=vout,
            value=100_000,
            mixdepth=0,
            address="bc1qtest",
            confirmations=6,
            scriptpubkey=scriptpubkey,
            path="m/84'/0'/0'/0/0",
        )

        return ClassifiedUTXO(
            utxo=utxo,
            status=status,
        )

    previous_input = TxInput.from_hex(
        txid="aa" * 32,
        vout=0,
        sequence=0xFFFFFFFE,
        value=100_000,
        scriptpubkey=scriptpubkey,
    )
    previous_output = TxOutput(value=100_000, script=bytes.fromhex(scriptpubkey))
    previous_tx = serialize_transaction(
        2,
        [previous_input],
        [previous_output],
        0,
    )
    deposit_txid = hash256(previous_tx)[::-1].hex()

    return [
        make_utxo("11" * 32, 0, "cj-out"),
        make_utxo("22" * 32, 1, "cj-change"),
        make_utxo(deposit_txid, 0, "deposit"),
        make_utxo("44" * 32, 0, "reserved"),
    ]


@pytest.fixture
def splice_prev_tx() -> bytes:
    scriptpubkey = pubkey_to_p2wpkh_script(CKey(b"\x01" * 32).pub)
    previous_input = TxInput.from_hex(
        txid="aa" * 32,
        vout=0,
        sequence=0xFFFFFFFE,
        value=100_000,
        scriptpubkey=scriptpubkey.hex(),
    )
    previous_output = TxOutput(value=100_000, script=scriptpubkey)
    return serialize_transaction(
        2,
        [previous_input],
        [previous_output],
        0,
    )
