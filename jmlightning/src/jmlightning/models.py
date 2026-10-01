from __future__ import annotations

from dataclasses import dataclass

from jmwallet.wallet.models import AddressStatus, UTXOInfo


@dataclass(frozen=True, slots=True)
class Outpoint:
    """A Bitcoin transaction outpoint."""

    txid: str
    vout: int

    def __post_init__(self) -> None:
        if not self.txid:
            raise ValueError("Transaction ID must not be empty")
        if self.vout < 0:
            raise ValueError("Output index must not be negative")

    def __str__(self) -> str:
        return f"{self.txid}:{self.vout}"

    def as_tuple(self) -> tuple[str, int]:
        """Return the representation expected by JoinMarket wallet APIs."""
        return self.txid, self.vout


@dataclass(frozen=True, slots=True)
class ClassifiedUTXO:
    """
    A JoinMarket UTXO together with its privacy classification.
    """

    utxo: UTXOInfo
    status: AddressStatus

    @property
    def outpoint(self) -> Outpoint:
        """Return this UTXO's canonical transaction outpoint."""
        return Outpoint(self.utxo.txid, self.utxo.vout)
