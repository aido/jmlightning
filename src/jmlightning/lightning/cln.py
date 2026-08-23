from dataclasses import dataclass

from jmcore.bitcoin import psbt_to_base64
from pyln.client import LightningRpc

from jmlightning.lightning.backend import FeePriority, LightningBackend


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
        Fetch fee estimates from CLN.

        Returns a list of FeeEstimate objects sorted by confirmation target.
        """
        try:
            result = self.rpc.estimatefees()
        except Exception as e:
            raise RuntimeError(f"Failed to retrieve fee estimates: {e}") from e

        return [
            FeeEstimate(
                blocks=entry["blocks"],
                sat_per_kvb=entry["feerate"],
            )
            for entry in result["feerates"]
        ]

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

    def send_psbt(self, psbt: bytes) -> dict[str, object]:
        """Finalize and broadcast a fully signed PSBT through CLN."""
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
            key=lambda e: abs(e.blocks - requested),
        )

        return estimate.sat_per_kvb / 1000.0

    @property
    def funding_output_type(self) -> str:
        return "p2wsh"
