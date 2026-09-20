from __future__ import annotations

from dataclasses import dataclass

from jmwallet.wallet.models import AddressStatus, UTXOInfo


@dataclass(frozen=True, slots=True)
class ClassifiedUTXO:
    """
    A JoinMarket UTXO together with its privacy classification.
    """

    utxo: UTXOInfo
    status: AddressStatus
