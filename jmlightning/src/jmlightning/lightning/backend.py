from abc import ABC, abstractmethod
from enum import StrEnum, auto
from typing import NotRequired, TypedDict


class FundChannelCompleteResult(TypedDict):
    commitments_secured: bool
    channel_id: NotRequired[str]


class SendPsbtResult(TypedDict):
    tx: str
    txid: str


class SpliceInitResult(TypedDict):
    psbt: str


class SpliceUpdateResult(TypedDict):
    psbt: str
    commitments_secured: bool
    signatures_secured: NotRequired[bool]


class SpliceSignedResult(TypedDict):
    tx: str
    txid: str
    psbt: str
    outnum: NotRequired[int]


class AddPsbtOutputResult(TypedDict):
    psbt: str
    estimated_added_weight: int
    outnum: int


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
        close_to: str | None = None,
    ) -> str:
        """
        Begin opening a channel and return the funding address.
        """

    @abstractmethod
    def open_channel_complete(
        self,
        peer_id: str,
        psbt: bytes,
    ) -> FundChannelCompleteResult:
        """
        Complete a channel open using the funding transaction PSBT.
        """

    @abstractmethod
    def send_psbt(self, psbt: bytes) -> SendPsbtResult:
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
    def splice_init(
        self,
        channel_id: str,
        amount: int,
        initial_psbt: bytes | None = None,
        feerate_per_kw: int | None = None,
        force_feerate: bool = False,
    ) -> SpliceInitResult:
        """
        Initiate a channel splice and return the resulting PSBT response.
        """

    @abstractmethod
    def add_psbt_output(
        self,
        amount: int,
        destination: str,
        initial_psbt: bytes | None = None,
    ) -> AddPsbtOutputResult:
        """Add a destination output to a PSBT using CLN's wallet.

        The output is created by CLN so its splice PSBT metadata, including
        the interactive transaction serial ID, remains authoritative.
        """

    @abstractmethod
    def splice_update(
        self,
        channel_id: str,
        psbt: bytes,
    ) -> SpliceUpdateResult:
        """
        Update the active splice with the supplied PSBT.
        """

    @abstractmethod
    def splice_signed(
        self,
        channel_id: str,
        psbt: bytes,
        sign_first: bool = False,
    ) -> SpliceSignedResult:
        """
        Complete an active splice using the fully signed PSBT.
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
    def get_splice_feerate_per_kw(self) -> int:
        """Return CLN's current splice feerate in sat/kw."""

    @abstractmethod
    def get_fee_rate(
        self,
        priority: FeePriority = FeePriority.NORMAL,
        feerate: str | int | None = None,
    ) -> float:
        """
        Returns a fee rate in sat/vbyte suitable for planner().
        """

    @property
    @abstractmethod
    def funding_output_type(self) -> str:
        """Script type used for channel funding."""
