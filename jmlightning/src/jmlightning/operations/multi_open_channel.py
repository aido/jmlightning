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

MultiOpenChannelConfirmationCallback = Callable[
    [list[tuple[str, int]], ExecutionPlan, ParsedTransaction, str],
    bool,
]


MultiOpenChannelPhase: TypeAlias = LifecyclePhase


class MultiOpenChannelCancelledError(RuntimeError):
    """Raised when the operator declines multi-channel funding."""


class MultiOpenChannelRecoveryRequiredError(RuntimeError):
    """Raised when multi-channel funding cannot be made safe automatically."""

    def __init__(self, message: str, *, peers: list[str], txid: str | None) -> None:
        super().__init__(message)
        self.peers = peers
        self.txid = txid


class MultiOpenChannelOperation:
    """Execute one CLN funding transaction for multiple peers."""

    required_capability = Capability.OPEN_CHANNEL

    def __init__(self, config: CLNConfig, cln_socket: Path) -> None:
        self.config = config
        self.cln_socket = cln_socket

    async def execute(
        self,
        destinations: list[tuple[str, int]],
        confirm: MultiOpenChannelConfirmationCallback | None = None,
    ) -> None:
        if not destinations:
            raise ValueError("At least one channel destination is required")

        peer_ids = [peer_id for peer_id, _ in destinations]
        if len(peer_ids) != len(set(peer_ids)):
            raise ValueError("Channel destinations must be unique")
        if any(amount <= 0 for _, amount in destinations):
            raise ValueError("Channel funding amounts must be positive")

        policy = PolicyEngine()
        planner = Planner()
        recovery_journal = RecoveryJournal(cast(Path, self.config.data_dir))
        recovery_id = recovery_journal.create(
            "multi_open_channel",
            {"peers": peer_ids},
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
        started: list[str] = []
        txid: str | None = None
        lifecycle = OperationLifecycle()
        operation_error: Exception | None = None
        cleanup_errors = lifecycle.cleanup_errors

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

            available = jmadapter.get_utxos(mixdepth=self.config.mixdepth)
            if not available:
                raise RuntimeError(
                    f"No eligible UTXOs available in mixdepth {self.config.mixdepth}"
                )

            # --------------------------------------------------------
            # Apply the capability policy
            # --------------------------------------------------------

            allowed = policy.filter(available, self.required_capability)
            if not allowed:
                raise RuntimeError(
                    f"No UTXOs in mixdepth {self.config.mixdepth} are permitted for "
                    f"{self.required_capability.name}"
                )

            allowed_outpoints = {(c.utxo.txid, c.utxo.vout) for c in allowed}
            classified = {(c.utxo.txid, c.utxo.vout): c for c in allowed}

            # --------------------------------------------------------
            # Fee rate
            # --------------------------------------------------------

            fee_rate = cln.get_fee_rate(self.config.fee_priority)
            target_amounts = [amount for _, amount in destinations]

            # --------------------------------------------------------
            # Selection and planning
            # --------------------------------------------------------

            selection_reserve = 0
            selection_input_types = ["p2wpkh"]
            previous_outpoints: set[tuple[str, int]] | None = None

            while True:
                # Reserve enough for the funding outputs and a conservative
                # one-input/multi-output transaction estimate. Planner then
                # recalculates the exact fee for the selected inputs.
                estimated_vsize = estimate_vsize(
                    input_types=selection_input_types,
                    output_types=[cln.funding_output_type] * len(destinations)
                    + ["p2wpkh"],
                )
                selection_target = (
                    sum(target_amounts)
                    + int(estimated_vsize * fee_rate + 0.999999)
                    + selection_reserve
                )

                selected_raw = jmadapter.select_utxos(
                    mixdepth=self.config.mixdepth,
                    target_amount=selection_target,
                    allowed_outpoints=allowed_outpoints,
                )
                try:
                    selected = [classified[(u.txid, u.vout)] for u in selected_raw]
                except KeyError as exc:
                    raise RuntimeError(
                        "JoinMarket selected a UTXO that was not present in the "
                        "policy-approved selection pool"
                    ) from exc

                if not selected:
                    raise RuntimeError("Unable to select UTXOs for OPEN_CHANNEL")

                policy.validate(selected, self.required_capability)
                current_outpoints = {
                    (coin.utxo.txid, coin.utxo.vout) for coin in selected
                }

                try:
                    plan = planner.build_multi_plan(
                        selected_coins=selected,
                        target_amounts=target_amounts,
                        fee_rate=fee_rate,
                        funding_output_types=[cln.funding_output_type]
                        * len(destinations),
                    )
                    break
                except ValueError as exc:
                    if str(exc) != "Insufficient funds after fees.":
                        raise
                    if current_outpoints == previous_outpoints:
                        raise
                    previous_outpoints = current_outpoints
                    selection_input_types = [
                        "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected
                    ]
                    selection_reserve += max(1, int(ceil(fee_rate)))

            for warning in plan.warnings:
                logger.warning(warning)
            logger.info(
                "Funding {} channels for {} sats, fee: {} sats, change: {} sats",
                len(destinations),
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
            # waits on CLN or operator input.
            jmadapter.start_lock_renewal()

            # --------------------------------------------------------
            # Start CLN funding for every channel
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            # Start every channel before constructing the shared transaction.
            # If any start fails, cancel every successful start before releasing
            # JoinMarket inputs.
            funding_addresses: list[str] = []
            for peer_id, amount in destinations:
                try:
                    funding_address = recovery_journal.call(
                        recovery_id,
                        action="fundchannel_start",
                        phase=LifecyclePhase.LOCKED.value,
                        fn=lambda: cln.open_channel_start(
                            peer_id=peer_id,
                            amount=amount,
                            announce=self.config.announce,
                        ),
                        locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                        owner_tokens=jmadapter._owner_tokens(),
                    )
                except Exception as exc:
                    cleanup_errors.extend(self._cancel_started_channels(cln, started))
                    lifecycle.release_locks = False
                    raise MultiOpenChannelRecoveryRequiredError(
                        "A channel start failed; CLN funding state may be "
                        "ambiguous; JoinMarket UTXOs remain locked for recovery",
                        peers=[*started, peer_id],
                        txid=None,
                    ) from exc
                started.append(peer_id)
                funding_addresses.append(funding_address)

            lifecycle.transition(LifecyclePhase.STARTED)
            lifecycle.release_locks = False

            # --------------------------------------------------------
            # Prepare the shared funding transaction
            # --------------------------------------------------------

            try:
                change_address = jmadapter.get_change_address(self.config.mixdepth)
                tx, txid, signed_psbt = tx_builder.build_and_sign_multifunding_tx(
                    plan=plan,
                    funding_addresses=funding_addresses,
                    change_address=change_address,
                    wallet=jmadapter.require_wallet(),
                )
            except Exception as exc:
                cleanup_errors.extend(self._cancel_started_channels(cln, started))
                lifecycle.release_locks = not cleanup_errors
                if lifecycle.release_locks:
                    lifecycle.transition(LifecyclePhase.LOCKED)
                raise MultiOpenChannelRecoveryRequiredError(
                    "Unable to prepare the shared funding transaction; "
                    "JoinMarket UTXOs remain locked for recovery"
                    if cleanup_errors
                    else "Shared funding transaction preparation failed",
                    peers=started,
                    txid=txid,
                ) from exc

            # --------------------------------------------------------
            # Optional operator confirmation
            # --------------------------------------------------------

            if confirm is not None and not confirm(destinations, plan, tx, txid):
                cleanup_errors.extend(self._cancel_started_channels(cln, started))
                if cleanup_errors:
                    lifecycle.release_locks = False
                    raise MultiOpenChannelRecoveryRequiredError(
                        "Unable to cancel all CLN channel funding after user decline; "
                        "JoinMarket UTXOs remain locked for recovery",
                        peers=started,
                        txid=txid,
                    )
                lifecycle.transition(LifecyclePhase.LOCKED)
                lifecycle.release_locks = True
                raise MultiOpenChannelCancelledError(
                    "Multi-channel funding cancelled by user",
                )

            # --------------------------------------------------------
            # Complete every channel against the shared transaction
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            # Complete each channel against the same signed transaction. CLN
            # records each channel as withheld until the shared PSBT is sent.
            for peer_id in started:
                try:

                    def complete_channel(peer_id: str = peer_id) -> dict[str, object]:
                        return cln.open_channel_complete(
                            peer_id=peer_id,
                            psbt=signed_psbt,
                        )

                    recovery_journal.call(
                        recovery_id,
                        action="fundchannel_complete",
                        phase=LifecyclePhase.STARTED.value,
                        fn=complete_channel,
                        locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                        owner_tokens=jmadapter._owner_tokens(),
                        psbt=signed_psbt,
                        txid=txid,
                    )
                except Exception as exc:
                    states, status_errors = self._get_funding_states(cln, started, txid)
                    cleanup_errors.extend(status_errors)
                    if (
                        any(state is ChannelFundingStatus.BROADCAST for state in states)
                        or cleanup_errors
                    ):
                        raise MultiOpenChannelRecoveryRequiredError(
                            "Multi-channel completion outcome is ambiguous; funding "
                            "may already have been broadcast",
                            peers=started,
                            txid=txid,
                        ) from exc
                    cleanup_errors.extend(self._cancel_started_channels(cln, started))
                    lifecycle.release_locks = not cleanup_errors
                    if lifecycle.release_locks:
                        lifecycle.transition(LifecyclePhase.LOCKED)
                    if cleanup_errors:
                        raise MultiOpenChannelRecoveryRequiredError(
                            "Unable to cancel CLN channel funding after completion "
                            "failure; JoinMarket UTXOs remain locked for recovery",
                            peers=started,
                            txid=txid,
                        ) from exc
                    raise

            lifecycle.transition(LifecyclePhase.WITHHELD)

            # --------------------------------------------------------
            # Broadcast the shared funding transaction through CLN
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
            except Exception as exc:
                states, status_errors = self._get_funding_states(cln, started, txid)
                cleanup_errors.extend(status_errors)

                if (
                    any(
                        state is ChannelFundingStatus.BROADCAST
                        for state in states.values()
                    )
                    or cleanup_errors
                ):
                    raise MultiOpenChannelRecoveryRequiredError(
                        "Unable to determine CLN broadcast outcome; JoinMarket "
                        "UTXOs remain locked for recovery",
                        peers=started,
                        txid=txid,
                    ) from exc

                withheld_peers = [
                    peer_id
                    for peer_id, state in states.items()
                    if state is ChannelFundingStatus.WITHHELD
                ]
                cleanup_errors.extend(
                    self._cancel_started_channels(cln, withheld_peers)
                )

                if cleanup_errors:
                    raise MultiOpenChannelRecoveryRequiredError(
                        "Unable to cancel withheld CLN channel funding; JoinMarket "
                        "UTXOs remain locked for recovery",
                        peers=started,
                        txid=txid,
                    ) from exc

                lifecycle.release_locks = True
                lifecycle.transition(LifecyclePhase.LOCKED)
                raise

            result_txid = broadcast_result.get("txid")
            if not isinstance(result_txid, str) or result_txid != txid:
                raise MultiOpenChannelRecoveryRequiredError(
                    "CLN returned an unexpected funding transaction id; JoinMarket "
                    "UTXOs remain locked for recovery",
                    peers=started,
                    txid=txid,
                )

            lifecycle.transition(LifecyclePhase.BROADCAST)
            lifecycle.release_locks = False
            logger.info("Funding transaction broadcast through CLN: {}", txid)
        except Exception as exc:
            operation_error = exc
            logger.error("Ah jaysus, failed to fund channels: {}", exc)
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
                    raise MultiOpenChannelRecoveryRequiredError(
                        "Multi-channel funding cleanup failed; "
                        "manual recovery is required",
                        peers=started,
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
                MultiOpenChannelRecoveryRequiredError,
                lambda error: MultiOpenChannelRecoveryRequiredError(
                    "Multi-channel funding failed and cleanup also failed; "
                    "manual recovery is required",
                    peers=started,
                    txid=txid,
                ),
            )

    @staticmethod
    def _cancel_started_channels(cln: CLNBackend, peers: list[str]) -> list[Exception]:
        errors: list[Exception] = []
        for peer_id in peers:
            try:
                cln.cancel_channel_funding(peer_id)
            except Exception as exc:
                errors.append(exc)
        return errors

    @staticmethod
    def _get_funding_states(
        cln: CLNBackend, peers: list[str], txid: str
    ) -> tuple[dict[str, ChannelFundingStatus], list[Exception]]:
        states: dict[str, ChannelFundingStatus] = {}
        errors: list[Exception] = []
        for peer_id in peers:
            try:
                states[peer_id] = cln.get_channel_funding_status(peer_id, txid)
            except Exception as exc:
                errors.append(exc)
        return states, errors


def confirm_multi_open_channel(
    destinations: list[tuple[str, int]],
    plan: ExecutionPlan,
    tx: ParsedTransaction,
    txid: str,
) -> bool:
    typer.echo("")
    typer.echo("Multi-channel funding transaction")
    typer.echo("=================================")
    typer.echo(f"Transaction ID: {txid}")
    typer.echo(f"Virtual size:   {plan.vsize} vbytes")
    typer.echo(f"Fee:            {plan.fee:,} sats")
    typer.echo("")
    typer.echo("Channels:")
    for peer_id, amount in destinations:
        typer.echo(f"  {peer_id}: {amount:,} sats")
    typer.echo("")
    typer.echo("Inputs:")
    for coin in plan.inputs:
        typer.echo(
            f"  {coin.utxo.txid}:{coin.utxo.vout} "
            f"{coin.utxo.value:,} sats ({coin.status})"
        )
    if plan.change > 0:
        typer.echo(f"\nJoinMarket change: {plan.change:,} sats")
    return typer.confirm("Proceed with multi-channel funding?", default=False)
