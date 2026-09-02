from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum, auto
from math import ceil
from pathlib import Path

import typer
from jmcore.bitcoin import ParsedTransaction, estimate_vsize
from loguru import logger

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.lightning.cln import CLNBackend
from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.policy import Capability, PolicyEngine
from jmlightning.tx_builder import TxBuilder

ConfirmationCallback = Callable[
    [str, ExecutionPlan, ParsedTransaction, str, str],
    bool,
]


class FundingPhase(StrEnum):
    PRESTART = auto()
    LOCKED = auto()
    STARTED = auto()
    WITHHELD = auto()
    BROADCAST = auto()


class FundingCancelledError(RuntimeError):
    """Raised when the operator declines channel funding."""


class FundingRecoveryRequiredError(RuntimeError):
    """Raised when funding state cannot be made safe automatically."""

    def __init__(
        self,
        message: str,
        *,
        peer_id: str,
        txid: str | None,
    ) -> None:
        super().__init__(message)
        self.peer_id = peer_id
        self.txid = txid


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
        confirm: ConfirmationCallback | None = None,
    ) -> None:
        policy = PolicyEngine()
        planner = Planner()
        jmadapter = JoinMarketAdapter(config=self.config)
        cln = CLNBackend(str(self.cln_socket))
        tx_builder = TxBuilder()

        selected: list[ClassifiedUTXO] = []
        locked: list[ClassifiedUTXO] = []
        phase = FundingPhase.PRESTART
        txid: str | None = None
        release_locks = True
        operation_error: Exception | None = None
        cleanup_errors: list[Exception] = []

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
                locked.append(coin)

            phase = FundingPhase.LOCKED

            logger.info(
                "Locked {} UTXOs for channel funding.",
                len(locked),
            )

            # --------------------------------------------------------
            # Start CLN funding
            # --------------------------------------------------------

            try:
                funding_address = cln.open_channel_start(
                    peer_id=peer_id,
                    amount=plan.amount,
                    announce=self.config.announce,
                )
            except Exception as exc:
                # We cannot know whether CLN accepted the RPC request.
                # There is no transaction id yet with which to identify
                # a possibly-created funding operation, so do not guess
                # and do not unlock the inputs.
                release_locks = False
                raise FundingRecoveryRequiredError(
                    "CLN fundchannel_start outcome is unknown; "
                    "JoinMarket UTXOs remain locked for recovery",
                    peer_id=peer_id,
                    txid=None,
                ) from exc

            phase = FundingPhase.STARTED
            release_locks = False

            logger.info(
                "CLN funding address obtained: {}",
                funding_address,
            )

            # --------------------------------------------------------
            # Prepare funding transaction while CLN funding is active
            # --------------------------------------------------------

            try:
                change_address = jmadapter.get_change_address(
                    self.config.mixdepth,
                )
                tx, txid, _, signed_psbt = tx_builder.build_and_sign_funding_tx(
                    plan=plan,
                    funding_address=funding_address,
                    change_address=change_address,
                    wallet=jmadapter.require_wallet(),
                )
            except Exception as exc:
                # fundchannel_start succeeded, so CLN now owns a live
                # funding operation. Never unlock JoinMarket inputs while
                # that operation may still exist.
                try:
                    cln.cancel_channel_funding(peer_id)
                except Exception as cancel_exc:
                    release_locks = False
                    raise FundingRecoveryRequiredError(
                        "Unable to cancel CLN channel funding after "
                        "local transaction preparation failed; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from cancel_exc

                phase = FundingPhase.LOCKED
                release_locks = True
                raise exc

            if confirm is not None and not confirm(
                peer_id,
                plan,
                tx,
                txid,
                funding_address,
            ):
                logger.info("Channel funding declined by user.")

                try:
                    cln.cancel_channel_funding(peer_id)
                except Exception as cancel_exc:
                    release_locks = False
                    raise FundingRecoveryRequiredError(
                        "Unable to cancel CLN channel funding after "
                        "user declined channel funding; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from cancel_exc

                phase = FundingPhase.LOCKED
                release_locks = True

                raise FundingCancelledError(
                    "Channel funding cancelled by user",
                )

            # --------------------------------------------------------
            # Complete CLN funding
            # --------------------------------------------------------

            try:
                cln.open_channel_complete(
                    peer_id=peer_id,
                    psbt=signed_psbt,
                )
            except Exception as exc:
                try:
                    status = cln.get_channel_funding_status(
                        peer_id=peer_id,
                        txid=txid,
                    )
                except Exception as status_exc:
                    release_locks = False
                    raise FundingRecoveryRequiredError(
                        "Unable to determine CLN channel completion state; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from status_exc

                if status is ChannelFundingStatus.WITHHELD:
                    release_locks = False
                    try:
                        cln.cancel_channel_funding(peer_id)
                    except Exception as cancel_exc:
                        raise FundingRecoveryRequiredError(
                            "Unable to cancel withheld CLN channel funding; "
                            "JoinMarket UTXOs remain locked for recovery",
                            peer_id=peer_id,
                            txid=txid,
                        ) from cancel_exc

                    release_locks = True
                    phase = FundingPhase.LOCKED
                elif status is ChannelFundingStatus.ABSENT:
                    release_locks = True
                    phase = FundingPhase.LOCKED
                else:
                    release_locks = False
                    raise FundingRecoveryRequiredError(
                        "CLN channel completion outcome is ambiguous; "
                        "funding may already have been broadcast",
                        peer_id=peer_id,
                        txid=txid,
                    ) from exc

                raise

            phase = FundingPhase.WITHHELD

            # --------------------------------------------------------
            # Broadcast through CLN
            # --------------------------------------------------------

            try:
                broadcast_result = cln.send_psbt(signed_psbt)
            except Exception:
                try:
                    status = cln.get_channel_funding_status(
                        peer_id=peer_id,
                        txid=txid,
                    )
                except Exception as status_exc:
                    release_locks = False
                    raise FundingRecoveryRequiredError(
                        "Unable to determine CLN sendpsbt outcome; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from status_exc

                if status is ChannelFundingStatus.WITHHELD:
                    release_locks = False
                    try:
                        cln.cancel_channel_funding(peer_id)
                    except Exception as cancel_exc:
                        raise FundingRecoveryRequiredError(
                            "Unable to cancel withheld CLN channel funding; "
                            "JoinMarket UTXOs remain locked for recovery",
                            peer_id=peer_id,
                            txid=txid,
                        ) from cancel_exc

                    release_locks = True
                    phase = FundingPhase.LOCKED
                    raise

                if status is ChannelFundingStatus.BROADCAST:
                    # The RPC response was lost, but CLN confirms that
                    # the expected funding transaction is no longer
                    # withheld. Never cancel and never unlock inputs
                    # after broadcast.
                    phase = FundingPhase.BROADCAST
                    release_locks = False
                    logger.warning(
                        "sendpsbt outcome was ambiguous, but CLN confirms "
                        "funding transaction {} was broadcast; treating "
                        "channel funding as successful",
                        txid,
                    )
                    return

                # ABSENT means the channel and transaction are both
                # absent from CLN's authoritative state.
                phase = FundingPhase.LOCKED
                release_locks = True
                raise

            result_txid = broadcast_result.get("txid")

            if not isinstance(result_txid, str) or result_txid != txid:
                release_locks = False
                raise FundingRecoveryRequiredError(
                    "CLN sendpsbt returned an unexpected funding transaction id; "
                    "JoinMarket UTXOs remain locked for recovery",
                    peer_id=peer_id,
                    txid=txid,
                )

            phase = FundingPhase.BROADCAST
            release_locks = False

            logger.info(
                "Funding transaction broadcast through CLN: {}",
                txid,
            )
        except FundingCancelledError:
            raise
        except Exception as exc:
            operation_error = exc
            logger.error(
                "Ah jaysus, failed to open channel: {}",
                exc,
            )
            raise

        finally:
            if release_locks:
                for coin in locked:
                    try:
                        jmadapter.unlock(coin)
                    except Exception as exc:
                        cleanup_errors.append(exc)
                        logger.error(
                            "Failed to unlock {}:{} after funding phase {}: {}",
                            coin.utxo.txid,
                            coin.utxo.vout,
                            phase,
                            exc,
                        )

            try:
                await jmadapter.close()
            except Exception as exc:
                cleanup_errors.append(exc)
                logger.error(
                    "Failed to close JoinMarket wallet after funding phase {}: {}",
                    phase,
                    exc,
                )

            if cleanup_errors and operation_error is None:
                if phase is not FundingPhase.BROADCAST:
                    raise FundingRecoveryRequiredError(
                        "Channel funding cleanup failed; manual recovery is required",
                        peer_id=peer_id,
                        txid=txid,
                    ) from cleanup_errors[0]

                logger.warning(
                    "Funding transaction {} was broadcast successfully, "
                    "but JoinMarket cleanup failed",
                    txid,
                )

            if cleanup_errors and operation_error is not None:
                logger.error(
                    "Channel funding failed and cleanup also failed; "
                    "manual recovery is required",
                )
                if not isinstance(operation_error, FundingRecoveryRequiredError):
                    raise FundingRecoveryRequiredError(
                        "Channel funding failed and cleanup also failed; "
                        "manual recovery is required",
                        peer_id=peer_id,
                        txid=txid,
                    ) from operation_error


def confirm_open_channel(
    peer_id: str,
    plan: ExecutionPlan,
    tx: ParsedTransaction,
    txid: str,
    funding_address: str,
) -> bool:
    typer.echo("")
    typer.echo("Channel funding transaction")
    typer.echo("==========================")
    typer.echo(f"Peer:             {peer_id}")
    typer.echo(f"Funding address:  {funding_address}")
    typer.echo(f"Funding amount:   {plan.amount:,} sats")
    typer.echo(f"Fee:              {plan.fee:,} sats")
    typer.echo(f"Virtual size:     {plan.vsize} vbytes")
    typer.echo(f"Transaction ID:   {txid}")
    typer.echo("")

    typer.echo("Inputs:")
    for txin, coin in zip(tx.inputs, plan.inputs, strict=True):
        typer.echo(
            f"  {coin.utxo.txid}:{coin.utxo.vout} "
            f"{coin.utxo.value:,} sats "
            f"({coin.status})"
        )

    typer.echo("")
    typer.echo("Outputs:")
    typer.echo(f"  Channel funding: {plan.amount:,} sats")

    if plan.change > 0:
        typer.echo(f"  JoinMarket change: {plan.change:,} sats")

    if plan.warnings:
        typer.echo("")
        typer.echo("Warnings:")
        for warning in plan.warnings:
            typer.echo(f"  WARNING: {warning}")

    typer.echo("")

    return typer.confirm(
        "Proceed with channel funding?",
        default=False,
    )
