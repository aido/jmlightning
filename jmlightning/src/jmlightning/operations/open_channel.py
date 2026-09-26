from __future__ import annotations

from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import TypeAlias, cast

import typer
from jmcore.bitcoin import ParsedTransaction, estimate_vsize
from loguru import logger

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.lightning.cln import CLNBackend
from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.lifecycle import LifecyclePhase, OperationLifecycle
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.policy import Capability, PolicyEngine
from jmlightning.recovery import RecoveryJournal
from jmlightning.tx_builder import TxBuilder

OpenChannelConfirmationCallback = Callable[
    [str, ExecutionPlan, ParsedTransaction, str, str],
    bool,
]


FundingPhase: TypeAlias = LifecyclePhase


class OpenChannelCancelledError(RuntimeError):
    """Raised when the operator declines channel funding."""


class OpenChannelRecoveryRequiredError(RuntimeError):
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
        confirm: OpenChannelConfirmationCallback | None = None,
    ) -> None:
        policy = PolicyEngine()
        planner = Planner()
        recovery_journal = RecoveryJournal(cast(Path, self.config.data_dir))
        recovery_id = recovery_journal.create(
            "open_channel",
            {"peer_id": peer_id},
        )
        jmadapter = JoinMarketAdapter(
            config=self.config,
            recovery_journal=recovery_journal,
            recovery_id=recovery_id,
        )
        cln = CLNBackend(str(self.cln_socket))
        tx_builder = TxBuilder()

        selected: list[ClassifiedUTXO] = []
        locked: list[ClassifiedUTXO] = []
        lifecycle = OperationLifecycle()
        txid: str | None = None
        operation_error: Exception | None = None

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

            lifecycle.transition(LifecyclePhase.LOCKED)

            # Keep the JoinMarket reservations alive while this operation
            # waits on CLN or operator input. Renewal runs independently of
            # the asyncio event loop because this operation may block it.
            jmadapter.start_lock_renewal()

            logger.info(
                "Locked {} UTXOs for channel funding.",
                len(locked),
            )

            # --------------------------------------------------------
            # Start CLN funding
            # --------------------------------------------------------

            # Renew immediately before handing the funding operation to CLN.
            # A failed renewal means we no longer have a valid reservation
            # for these inputs and must not continue with them.
            jmadapter.renew_locks(locked)

            try:
                funding_address = recovery_journal.call(
                    recovery_id,
                    action="fundchannel_start",
                    phase=LifecyclePhase.LOCKED.value,
                    fn=lambda: cln.open_channel_start(
                        peer_id=peer_id,
                        amount=plan.amount,
                        announce=self.config.announce,
                    ),
                    locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                    owner_tokens=jmadapter._owner_tokens(),
                )
            except Exception as exc:
                # We cannot know whether CLN accepted the RPC request.
                # There is no transaction id yet with which to identify
                # a possibly-created funding operation, so do not guess
                # and do not unlock the inputs.
                lifecycle.release_locks = False
                raise OpenChannelRecoveryRequiredError(
                    "CLN fundchannel_start outcome is unknown; "
                    "JoinMarket UTXOs remain locked for recovery",
                    peer_id=peer_id,
                    txid=None,
                ) from exc

            lifecycle.transition(LifecyclePhase.STARTED)
            lifecycle.release_locks = False

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
                    recovery_journal.call(
                        recovery_id,
                        action="fundchannel_cancel",
                        phase=lifecycle.phase.value,
                        fn=lambda: cln.cancel_channel_funding(peer_id),
                        locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                        owner_tokens=jmadapter._owner_tokens(),
                        txid=txid,
                    )
                except Exception as cancel_exc:
                    lifecycle.release_locks = False
                    raise OpenChannelRecoveryRequiredError(
                        "Unable to cancel CLN channel funding after "
                        "local transaction preparation failed; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from cancel_exc

                lifecycle.transition(LifecyclePhase.LOCKED)
                lifecycle.release_locks = True
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
                    recovery_journal.call(
                        recovery_id,
                        action="fundchannel_cancel",
                        phase=lifecycle.phase.value,
                        fn=lambda: cln.cancel_channel_funding(peer_id),
                        locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                        owner_tokens=jmadapter._owner_tokens(),
                        txid=txid,
                    )
                except Exception as cancel_exc:
                    lifecycle.release_locks = False
                    raise OpenChannelRecoveryRequiredError(
                        "Unable to cancel CLN channel funding after "
                        "user declined channel funding; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from cancel_exc

                lifecycle.transition(LifecyclePhase.LOCKED)
                lifecycle.release_locks = True

                raise OpenChannelCancelledError(
                    "Channel funding cancelled by user",
                )

            # --------------------------------------------------------
            # Complete CLN funding
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            try:
                recovery_journal.call(
                    recovery_id,
                    action="fundchannel_complete",
                    phase=LifecyclePhase.STARTED.value,
                    fn=lambda: cln.open_channel_complete(
                        peer_id=peer_id,
                        psbt=signed_psbt,
                    ),
                    locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                    owner_tokens=jmadapter._owner_tokens(),
                    psbt=signed_psbt,
                    txid=txid,
                )
            except Exception as exc:
                try:
                    status = cln.get_channel_funding_status(
                        peer_id=peer_id,
                        txid=txid,
                    )
                except Exception as status_exc:
                    lifecycle.release_locks = False
                    raise OpenChannelRecoveryRequiredError(
                        "Unable to determine CLN channel completion state; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from status_exc

                if status is ChannelFundingStatus.WITHHELD:
                    lifecycle.release_locks = False
                    try:
                        recovery_journal.call(
                            recovery_id,
                            action="fundchannel_cancel",
                            phase=lifecycle.phase.value,
                            fn=lambda: cln.cancel_channel_funding(peer_id),
                            locked_outpoints=[
                                (c.utxo.txid, c.utxo.vout) for c in locked
                            ],
                            owner_tokens=jmadapter._owner_tokens(),
                            txid=txid,
                        )
                    except Exception as cancel_exc:
                        raise OpenChannelRecoveryRequiredError(
                            "Unable to cancel withheld CLN channel funding; "
                            "JoinMarket UTXOs remain locked for recovery",
                            peer_id=peer_id,
                            txid=txid,
                        ) from cancel_exc

                    lifecycle.release_locks = True
                    lifecycle.transition(LifecyclePhase.LOCKED)
                elif status is ChannelFundingStatus.ABSENT:
                    lifecycle.release_locks = True
                    lifecycle.transition(LifecyclePhase.LOCKED)
                else:
                    lifecycle.release_locks = False
                    raise OpenChannelRecoveryRequiredError(
                        "CLN channel completion outcome is ambiguous; "
                        "funding may already have been broadcast",
                        peer_id=peer_id,
                        txid=txid,
                    ) from exc

                raise

            lifecycle.transition(LifecyclePhase.WITHHELD)

            # --------------------------------------------------------
            # Broadcast through CLN
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            try:
                broadcast_result = recovery_journal.call(
                    recovery_id,
                    action="sendpsbt",
                    phase=LifecyclePhase.WITHHELD.value,
                    fn=lambda: cln.send_psbt(signed_psbt),
                    locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                    owner_tokens=jmadapter._owner_tokens(),
                    psbt=signed_psbt,
                    txid=txid,
                )
            except Exception:
                try:
                    status = cln.get_channel_funding_status(
                        peer_id=peer_id,
                        txid=txid,
                    )
                except Exception as status_exc:
                    lifecycle.release_locks = False
                    raise OpenChannelRecoveryRequiredError(
                        "Unable to determine CLN sendpsbt outcome; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peer_id=peer_id,
                        txid=txid,
                    ) from status_exc

                if status is ChannelFundingStatus.WITHHELD:
                    lifecycle.release_locks = False
                    try:
                        recovery_journal.call(
                            recovery_id,
                            action="fundchannel_cancel",
                            phase=lifecycle.phase.value,
                            fn=lambda: cln.cancel_channel_funding(peer_id),
                            locked_outpoints=[
                                (c.utxo.txid, c.utxo.vout) for c in locked
                            ],
                            owner_tokens=jmadapter._owner_tokens(),
                            txid=txid,
                        )
                    except Exception as cancel_exc:
                        raise OpenChannelRecoveryRequiredError(
                            "Unable to cancel withheld CLN channel funding; "
                            "JoinMarket UTXOs remain locked for recovery",
                            peer_id=peer_id,
                            txid=txid,
                        ) from cancel_exc

                    lifecycle.release_locks = True
                    lifecycle.transition(LifecyclePhase.LOCKED)
                    raise

                if status is ChannelFundingStatus.BROADCAST:
                    # The RPC response was lost, but CLN confirms that
                    # the expected funding transaction is no longer
                    # withheld. Never cancel and never unlock inputs
                    # after broadcast.
                    lifecycle.transition(LifecyclePhase.BROADCAST)
                    lifecycle.release_locks = False
                    logger.warning(
                        "sendpsbt outcome was ambiguous, but CLN confirms "
                        "funding transaction {} was broadcast; treating "
                        "channel funding as successful",
                        txid,
                    )
                    return

                # ABSENT means the channel and transaction are both
                # absent from CLN's authoritative state.
                lifecycle.transition(LifecyclePhase.LOCKED)
                lifecycle.release_locks = True
                raise

            result_txid = broadcast_result.get("txid")

            if not isinstance(result_txid, str) or result_txid != txid:
                lifecycle.release_locks = False
                raise OpenChannelRecoveryRequiredError(
                    "CLN sendpsbt returned an unexpected funding transaction id; "
                    "JoinMarket UTXOs remain locked for recovery",
                    peer_id=peer_id,
                    txid=txid,
                )

            lifecycle.transition(LifecyclePhase.BROADCAST)
            lifecycle.release_locks = False

            logger.info(
                "Funding transaction broadcast through CLN: {}",
                txid,
            )
        except OpenChannelCancelledError:
            raise
        except Exception as exc:
            operation_error = exc
            logger.error(
                "Ah jaysus, failed to open channel: {}",
                exc,
            )
            raise

        finally:
            await lifecycle.cleanup(
                locked=locked,
                adapter=jmadapter,
                close_message="Failed to close JoinMarket wallet after funding",
                unlock_message="Failed to unlock",
            )

            if lifecycle.cleanup_errors and operation_error is None:
                if lifecycle.phase is not LifecyclePhase.BROADCAST:
                    raise OpenChannelRecoveryRequiredError(
                        "Channel funding cleanup failed; manual recovery is required",
                        peer_id=peer_id,
                        txid=txid,
                    ) from lifecycle.cleanup_errors[0]

                logger.warning(
                    "Funding transaction {} was broadcast successfully, "
                    "but JoinMarket cleanup failed",
                    txid,
                )

            lifecycle.resolve_if_clean(
                recovery_journal,
                recovery_id,
                terminal_phase=LifecyclePhase.BROADCAST,
            )

            lifecycle.raise_recovery_if_needed(
                operation_error,
                OpenChannelRecoveryRequiredError,
                lambda error: OpenChannelRecoveryRequiredError(
                    "Channel funding failed and cleanup also failed; "
                    "manual recovery is required",
                    peer_id=peer_id,
                    txid=txid,
                ),
            )


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
