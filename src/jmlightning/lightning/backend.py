from abc import ABC, abstractmethod
from enum import StrEnum, auto
from typing import TypeAlias

RPCResponse: TypeAlias = dict[str, object]


class FeePriority(StrEnum):
    HIGH = auto()
    NORMAL = auto()
    ECONOMY = auto()


class ChannelFundingStatus(StrEnum):
    ABSENT = auto()
    WITHHELD = auto()
    BROADCAST = auto()


class LightningBackend(ABC):
    @abstractmethod
    def open_channel_start(
        self,
        peer_id: str,
        amount: int,
        announce: bool = False,
    ) -> str:
        """
        Begin opening a channel and return the funding address.
        """

    @abstractmethod
    def open_channel_complete(
        self,
        peer_id: str,
        psbt: bytes,
    ) -> RPCResponse:
        """
        Complete a channel open using the funding transaction PSBT.
        """

    @abstractmethod
    def send_psbt(self, psbt: bytes) -> RPCResponse:
        """
        Finalise and broadcast a fully signed PSBT.
        """

    @abstractmethod
    def cancel_channel_funding(
        self,
        peer_id: str,
    ) -> None:
        """
        Cancel a channel funding operation before its funding transaction is broadcast.
        """

    @abstractmethod
    def get_channel_funding_status(
        self,
        peer_id: str,
        txid: str,
    ) -> ChannelFundingStatus:
        """
        Determine whether the expected funding transaction is withheld or broadcast.
        """

    @abstractmethod
    def get_fee_rate(
        self,
        priority: FeePriority = FeePriority.NORMAL,
    ) -> float:
        """
        Returns a fee rate in sat/vbyte suitable for planner().
        """

    @property
    @abstractmethod
    def funding_output_type(self) -> str:
        """Script type used for channel funding."""
