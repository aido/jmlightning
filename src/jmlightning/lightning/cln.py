from dataclasses import dataclass
from math import isfinite

from jmcore.bitcoin import psbt_to_base64
from pyln.client import LightningRpc

from jmlightning.lightning.backend import (
    ChannelFundingStatus,
    FeePriority,
    LightningBackend,
)


@dataclass(frozen=True)
class FeeEstimate:
    blocks: int
    sat_per_kvb: int


class CLNBackend(LightningBackend):
    def __init__(self, socket_path: str):
        # Hooks up to the Unix socket we passed in config
        self.rpc = LightningRpc(socket_path)

    def _estimate_fees(self) -> list[FeeEstimate]:
        """
        Fetch and validate fee estimates from CLN.

        Returns a list of FeeEstimate objects sorted by confirmation target.
        """
        try:
            result = self.rpc.estimatefees()
        except Exception as exc:
            raise RuntimeError(f"Failed to retrieve fee estimates: {exc}") from exc

        if not isinstance(result, dict):
            raise RuntimeError("CLN estimatefees returned an invalid response")

        raw_feerates = result.get("feerates")

        if not isinstance(raw_feerates, list) or not raw_feerates:
            raise RuntimeError(
                "CLN estimatefees returned invalid or empty feerates data"
            )

        estimates: list[FeeEstimate] = []

        for entry in raw_feerates:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    "CLN estimatefees returned an invalid fee estimate entry"
                )

            blocks = entry.get("blocks")
            sat_per_kvb = entry.get("feerate")

            if not isinstance(blocks, int) or isinstance(blocks, bool) or blocks <= 0:
                raise RuntimeError(
                    "CLN estimatefees returned an invalid confirmation target"
                )

            if (
                not isinstance(sat_per_kvb, int)
                or isinstance(sat_per_kvb, bool)
                or sat_per_kvb <= 0
            ):
                raise RuntimeError("CLN estimatefees returned an invalid fee rate")

            estimates.append(
                FeeEstimate(
                    blocks=blocks,
                    sat_per_kvb=sat_per_kvb,
                )
            )

        return estimates

    def open_channel_start(
        self,
        peer_id: str,
        amount: int,
        announce: bool = False,
    ) -> str:
        """Ask CLN for the 2-of-2 multisig address to fund the channel."""
        try:
            result = self.rpc.fundchannel_start(
                peer_id,
                amount,
                announce=announce,
            )
            funding_address = result.get("funding_address")

            if not isinstance(funding_address, str):
                raise RuntimeError(
                    "CLN fundchannel_start response is missing funding_address"
                )

            return funding_address
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to start channel open: {exc}") from exc

    def open_channel_complete(
        self,
        peer_id: str,
        psbt: bytes,
    ) -> dict[str, object]:
        """Complete channel establishment using the funding transaction PSBT."""
        try:
            result = self.rpc.fundchannel_complete(
                node_id=peer_id,
                psbt=psbt_to_base64(psbt),
                withhold=True,
            )
            return dict(result)
        except Exception as exc:
            raise RuntimeError(f"Failed to complete channel open: {exc}") from exc

    def cancel_channel_funding(
        self,
        peer_id: str,
    ) -> None:
        """Cancel a funding operation before its funding transaction is broadcast."""
        try:
            self.rpc.fundchannel_cancel(node_id=peer_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to cancel channel funding: {exc}") from exc

    def splice_init(
        self,
        channel_id: str,
        amount: int,
        initial_psbt: bytes | None = None,
        feerate_per_kw: int | None = None,
        force_feerate: bool = False,
    ) -> dict[str, object]:
        """Initiate a CLN channel splice."""
        try:
            kwargs: dict[str, object] = {
                "channel_id": channel_id,
                "amount": amount,
                "force_feerate": force_feerate,
            }

            if initial_psbt is not None:
                kwargs["initialpsbt"] = psbt_to_base64(initial_psbt)

            if feerate_per_kw is not None:
                kwargs["feerate_per_kw"] = feerate_per_kw

            result = self.rpc.splice_init(**kwargs)

            if not isinstance(result, dict):
                raise RuntimeError("CLN splice_init returned an invalid response")

            psbt = result.get("psbt")
            if not isinstance(psbt, str) or not psbt:
                raise RuntimeError("CLN splice_init response is missing psbt")

            return dict(result)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to initiate channel splice: {exc}") from exc

    def splice_update(
        self,
        channel_id: str,
        psbt: bytes,
    ) -> dict[str, object]:
        """Update an active CLN channel splice."""
        try:
            result = self.rpc.splice_update(
                channel_id=channel_id,
                psbt=psbt_to_base64(psbt),
            )

            if not isinstance(result, dict):
                raise RuntimeError("CLN splice_update returned an invalid response")

            returned_psbt = result.get("psbt")
            if not isinstance(returned_psbt, str) or not returned_psbt:
                raise RuntimeError("CLN splice_update response is missing psbt")

            commitments_secured = result.get("commitments_secured")
            if not isinstance(commitments_secured, bool):
                raise RuntimeError(
                    "CLN splice_update response has invalid commitments_secured"
                )

            if "signatures_secured" in result and not isinstance(
                result["signatures_secured"], bool
            ):
                raise RuntimeError(
                    "CLN splice_update response has invalid signatures_secured"
                )

            return dict(result)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to update channel splice: {exc}") from exc

    def splice_signed(
        self,
        channel_id: str,
        psbt: bytes,
        sign_first: bool = False,
    ) -> dict[str, object]:
        """Complete an active CLN channel splice."""
        try:
            result = self.rpc.splice_signed(
                channel_id=channel_id,
                psbt=psbt_to_base64(psbt),
                sign_first=sign_first,
            )

            if not isinstance(result, dict):
                raise RuntimeError("CLN splice_signed returned an invalid response")

            for field in ("tx", "txid", "psbt"):
                value = result.get(field)
                if not isinstance(value, str) or not value:
                    raise RuntimeError(f"CLN splice_signed response is missing {field}")

            outnum = result.get("outnum")
            if outnum is not None and (
                not isinstance(outnum, int) or isinstance(outnum, bool) or outnum < 0
            ):
                raise RuntimeError("CLN splice_signed response has invalid outnum")

            return dict(result)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to complete channel splice: {exc}") from exc

    def get_channel_funding_status(
        self,
        peer_id: str,
        txid: str,
    ) -> ChannelFundingStatus:
        """
        Determine the CLN funding state for the expected transaction.

        ``fundchannel_complete(withhold=True)`` records the channel and
        marks it withheld. ``sendpsbt`` clears that flag and associates
        the transaction with the channel. ``listtransactions`` provides
        a second source of truth for an already-broadcast transaction.
        """
        try:
            peer_result = self.rpc.listpeerchannels(peer_id)
            if not isinstance(peer_result, dict):
                raise RuntimeError("CLN listpeerchannels returned an invalid response")

            channels = peer_result.get("channels")
            if channels is None:
                channels = []
            elif not isinstance(channels, list):
                raise RuntimeError(
                    "CLN listpeerchannels returned invalid channels data"
                )

            for channel in channels:
                if not isinstance(channel, dict):
                    raise RuntimeError(
                        "CLN listpeerchannels returned an invalid channel entry"
                    )

                if channel.get("funding_txid") == txid:
                    funding = channel.get("funding")
                    if isinstance(funding, dict) and funding.get("withheld") is True:
                        return ChannelFundingStatus.WITHHELD

                    # A matching channel which is no longer withheld is
                    # deliberately treated as broadcast/released. This is
                    # the conservative state for cancellation and UTXO
                    # unlocking: never attempt to cancel or reuse inputs.
                    return ChannelFundingStatus.BROADCAST

                inflight = channel.get("inflight")
                if inflight is None:
                    continue
                if not isinstance(inflight, list):
                    raise RuntimeError(
                        "CLN listpeerchannels returned invalid inflight data"
                    )

                for candidate in inflight:
                    if not isinstance(candidate, dict):
                        raise RuntimeError(
                            "CLN listpeerchannels returned an invalid "
                            "inflight channel entry"
                        )
                    if candidate.get("funding_txid") == txid:
                        return ChannelFundingStatus.WITHHELD

            transaction_result = self.rpc.listtransactions()
            if not isinstance(transaction_result, dict):
                raise RuntimeError("CLN listtransactions returned an invalid response")

            transactions = transaction_result.get("transactions")
            if transactions is None:
                transactions = []
            elif not isinstance(transactions, list):
                raise RuntimeError(
                    "CLN listtransactions returned invalid transactions data"
                )

            for transaction in transactions:
                if not isinstance(transaction, dict):
                    raise RuntimeError(
                        "CLN listtransactions returned an invalid transaction entry"
                    )
                if transaction.get("hash") == txid:
                    return ChannelFundingStatus.BROADCAST

            return ChannelFundingStatus.ABSENT

        except Exception as exc:
            raise RuntimeError(
                f"Failed to determine CLN funding status for {peer_id}: {exc}"
            ) from exc

    def send_psbt(self, psbt: bytes) -> dict[str, object]:
        """Finalise and broadcast a fully signed PSBT through CLN."""
        try:
            result = self.rpc.sendpsbt(
                psbt=psbt_to_base64(psbt),
            )
            return dict(result)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to send funding PSBT through CLN: {exc}"
            ) from exc

    def get_fee_rate(
        self,
        priority: FeePriority = FeePriority.NORMAL,
    ) -> float:
        """
        Returns a fee rate in sat/vbyte suitable for planner().
        """
        estimates = self._estimate_fees()

        mapping = {
            FeePriority.HIGH: 2,
            FeePriority.NORMAL: 6,
            FeePriority.ECONOMY: 12,
        }

        requested = mapping[priority]

        estimate = min(
            estimates,
            key=lambda estimate: abs(estimate.blocks - requested),
        )

        fee_rate = estimate.sat_per_kvb / 1000.0

        if not isfinite(fee_rate) or fee_rate <= 0:
            raise RuntimeError("CLN returned an invalid fee rate")

        return fee_rate

    @property
    def funding_output_type(self) -> str:
        return "p2wsh"
