import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import pubkey_to_p2wpkh_script
from jmwallet.wallet.models import AddressStatus, UTXOInfo

from jmlightning.models import ClassifiedUTXO
from jmlightning.policy import PolicyEngine


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def classified_utxos() -> list[ClassifiedUTXO]:
    scriptpubkey = pubkey_to_p2wpkh_script(
        PrivateKey(b"\x01" * 32).public_key.format(compressed=True)
    ).hex()

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

    return [
        make_utxo("11" * 32, 0, "cj-out"),
        make_utxo("22" * 32, 1, "cj-change"),
        make_utxo("33" * 32, 0, "deposit"),
        make_utxo("44" * 32, 0, "reserved"),
    ]
