from __future__ import annotations

from math import ceil
from pathlib import Path

from jmcore.bitcoin import estimate_vsize
from loguru import logger

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.cln import CLNBackend
from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import Planner
from jmlightning.policy import Capability, PolicyEngine
from jmlightning.tx_builder import TxBuilder


class OpenChannelOperation:
    """
    Execute a CLN channel-opening operation using JoinMarket UTXOs.

    UTXO eligibility is determined exclusively by the jm-lightning
    capability policy. The JoinMarket adapter is deliberately unaware
    of operation-specific policy.
    """

    required_capability = Capability.OPEN_CHANNEL

    def __init__(
        self,
        config: CLNConfig,
        cln_socket: Path,
    ) -> None:
        self.config = config
        self.cln_socket = cln_socket

    async def execute(
        self,
        peer_id: str,
    ) -> None:
        policy = PolicyEngine()
        planner = Planner()
        jmadapter = JoinMarketAdapter(config=self.config)
        cln = CLNBackend(str(self.cln_socket))
        tx_builder = TxBuilder()

        selected: list[ClassifiedUTXO] = []
        channel_funding_started = False

        try:
            # --------------------------------------------------------
            # Connect and synchronise JoinMarket
            # --------------------------------------------------------

            logger.info("Connecting to JoinMarket wallet...")
            await jmadapter.connect()
            logger.info("JoinMarket wallet synchronised.")

            # --------------------------------------------------------
            # Get eligible JoinMarket UTXOs
            # --------------------------------------------------------

            logger.info("Fetching eligible JoinMarket UTXOs...")

            available = jmadapter.get_utxos(
                mixdepth=self.config.mixdepth,
            )

            if not available:
                raise RuntimeError(
                    f"No eligible UTXOs available in mixdepth {self.config.mixdepth}"
                )

            logger.info(
                "JoinMarket returned {} eligible UTXOs.",
                len(available),
            )

            # --------------------------------------------------------
            # Apply the capability policy
            # --------------------------------------------------------

            capability = self.required_capability

            allowed = policy.filter(
                available,
                capability,
            )

            if not allowed:
                raise RuntimeError(
                    f"No UTXOs in mixdepth {self.config.mixdepth} "
                    f"are permitted for {capability.name}"
                )

            logger.info(
                "{} of {} UTXOs permitted for {}.",
                len(allowed),
                len(available),
                capability.name,
            )

            allowed_outpoints = {(coin.utxo.txid, coin.utxo.vout) for coin in allowed}

            classified_by_outpoint = {
                (coin.utxo.txid, coin.utxo.vout): coin for coin in allowed
            }

            # --------------------------------------------------------
            # Fee rate
            # --------------------------------------------------------

            fee_rate = cln.get_fee_rate(
                self.config.fee_priority,
            )

            logger.info(
                "Using fee rate: {:.3f} sat/vB",
                fee_rate,
            )

            # --------------------------------------------------------
            # Selection and planning
            # --------------------------------------------------------

            if self.config.amount == 0:
                selected = list(allowed)

                if not selected:
                    raise RuntimeError(f"Unable to select UTXOs for {capability.name}")

                policy.validate(
                    selected,
                    capability,
                )

                plan = planner.build_plan(
                    selected_coins=selected,
                    target_amount=0,
                    fee_rate=fee_rate,
                    funding_output_type=cln.funding_output_type,
                )

            else:
                selection_target = self.config.amount
                previous_outpoints: set[tuple[str, int]] | None = None

                while True:
                    selected_raw = jmadapter.select_utxos(
                        mixdepth=self.config.mixdepth,
                        target_amount=selection_target,
                        allowed_outpoints=allowed_outpoints,
                    )

                    try:
                        selected = [
                            classified_by_outpoint[(utxo.txid, utxo.vout)]
                            for utxo in selected_raw
                        ]
                    except KeyError as exc:
                        raise RuntimeError(
                            "JoinMarket selected a UTXO that was not "
                            "present in the policy-approved selection pool"
                        ) from exc

                    if not selected:
                        raise RuntimeError(
                            f"Unable to select UTXOs for {capability.name}"
                        )

                    policy.validate(
                        selected,
                        capability,
                    )

                    current_outpoints = {
                        (coin.utxo.txid, coin.utxo.vout) for coin in selected
                    }

                    try:
                        plan = planner.build_plan(
                            selected_coins=selected,
                            target_amount=self.config.amount,
                            fee_rate=fee_rate,
                            funding_output_type=cln.funding_output_type,
                        )
                        break

                    except ValueError as exc:
                        if str(exc) != "Insufficient funds after fees.":
                            raise

                        if previous_outpoints == current_outpoints:
                            raise

                        previous_outpoints = current_outpoints

                        input_types = [
                            "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh"
                            for coin in selected
                        ]

                        vsize = estimate_vsize(
                            input_types=input_types,
                            output_types=[
                                cln.funding_output_type,
                                "p2wpkh",
                            ],
                        )

                        estimated_fee = ceil(vsize * fee_rate)

                        selection_target = self.config.amount + estimated_fee

            for warning in plan.warnings:
                logger.warning(warning)

            logger.info(
                "Funding amount: {} sats, fee: {} sats, change: {} sats",
                plan.amount,
                plan.fee,
                plan.change,
            )

            # --------------------------------------------------------
            # Lock selected UTXOs
            # --------------------------------------------------------

            for coin in selected:
                jmadapter.lock(coin)

            logger.info(
                "Locked {} UTXOs for channel funding.",
                len(selected),
            )

            # --------------------------------------------------------
            # Start CLN funding
            # --------------------------------------------------------

            funding_address = cln.open_channel_start(
                peer_id=peer_id,
                amount=plan.amount,
                announce=self.config.announce,
            )

            channel_funding_started = True

            logger.info(
                "CLN funding address obtained: {}",
                funding_address,
            )

            # --------------------------------------------------------
            # JoinMarket change address
            # --------------------------------------------------------

            change_address = jmadapter.get_change_address(
                self.config.mixdepth,
            )

            # --------------------------------------------------------
            # Build and sign funding transaction
            # --------------------------------------------------------

            _, txid, _, signed_psbt = tx_builder.build_and_sign_funding_tx(
                plan=plan,
                funding_address=funding_address,
                change_address=change_address,
                wallet=jmadapter.require_wallet(),
            )

            # --------------------------------------------------------
            # Complete CLN funding
            # --------------------------------------------------------

            cln.open_channel_complete(
                peer_id=peer_id,
                psbt=signed_psbt,
            )

            # --------------------------------------------------------
            # Broadcast through CLN
            # --------------------------------------------------------

            broadcast_result = cln.send_psbt(signed_psbt)

            # At this point CLN has accepted the PSBT for broadcast, so
            # there is no longer a withheld funding operation to cancel.
            channel_funding_started = False

            logger.info(
                "Funding transaction broadcast through CLN: {}",
                broadcast_result.get("txid", txid),
            )

        except Exception as exc:
            logger.error(
                "Ah jaysus, failed to open channel: {}",
                exc,
            )

            if channel_funding_started:
                try:
                    cln.rpc.fundchannel_cancel(
                        node_id=peer_id,
                    )
                except Exception as cancel_exc:
                    logger.warning(
                        "Failed to cancel pending CLN channel funding: {}",
                        cancel_exc,
                    )

            raise

        finally:
            for coin in selected:
                try:
                    jmadapter.unlock(coin)
                except Exception as exc:
                    logger.warning(
                        "Failed to unlock {}:{}: {}",
                        coin.utxo.txid,
                        coin.utxo.vout,
                        exc,
                    )

            await jmadapter.close()
