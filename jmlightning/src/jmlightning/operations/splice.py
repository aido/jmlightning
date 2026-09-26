from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import replace
from math import ceil
from pathlib import Path
from typing import TypeAlias, cast

import typer
from jmcore.bitcoin import estimate_vsize
from loguru import logger

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.cln import CLNBackend
from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.lifecycle import LifecyclePhase, OperationLifecycle
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.policy import Capability, PolicyEngine
from jmlightning.recovery import RecoveryJournal
from jmlightning.tx_builder import TxBuilder

SpliceConfirmationCallback = Callable[
    [str, ExecutionPlan, bytes],
    bool,
]


SplicePhase: TypeAlias = LifecyclePhase


class SpliceRecoveryRequiredError(RuntimeError):
    """Raised when splice state cannot be made safe automatically."""

    def __init__(
        self,
        message: str,
        *,
        channel_id: str,
        txid: str | None,
        locked_outpoints: tuple[tuple[str, int], ...],
    ) -> None:
        super().__init__(message)
        self.channel_id = channel_id
        self.txid = txid
        self.locked_outpoints = locked_outpoints


class SpliceOperation:
    """
    Execute a CLN channel splice-in using JoinMarket UTXOs.

    UTXO eligibility is determined exclusively by the jm-lightning
    capability policy. The JoinMarket adapter is deliberately unaware
    of operation-specific policy.
    """

    required_capability = Capability.SPLICE

    def __init__(
        self,
        config: CLNConfig,
        cln_socket: Path,
    ) -> None:
        self.config = config
        self.cln_socket = cln_socket

    async def execute(
        self,
        channel_id: str,
        confirm: SpliceConfirmationCallback | None = None,
    ) -> str:
        if self.config.amount <= 0:
            raise ValueError(
                "Splice-in amount must be greater than zero; "
                "splice sweep is not supported"
            )

        policy = PolicyEngine()
        planner = Planner()
        recovery_journal = RecoveryJournal(cast(Path, self.config.data_dir))
        recovery_id = recovery_journal.create(
            "splice",
            {"channel_id": channel_id},
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

            splice_feerate_per_kw = cln.get_splice_feerate_per_kw()

            logger.info(
                "Using CLN splice fee rate: {} sat/kw ({:.3f} sat/vB)",
                splice_feerate_per_kw,
                splice_feerate_per_kw / 250.0,
            )

            # --------------------------------------------------------
            # Selection and planning
            # --------------------------------------------------------

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

                if len(selected) != 1:
                    raise RuntimeError(
                        "Splice-in requires exactly one policy-approved JoinMarket UTXO"
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
                        fee_rate=splice_feerate_per_kw / 250.0,
                        funding_output_type=cln.funding_output_type,
                    )

                    # The normal planner does not know about the existing
                    # channel 2-of-2 input that CLN charges to the initiator.
                    # Reserve its CLN weight before committing to this UTXO.
                    conservative_vsize = plan.vsize + 97
                    conservative_fee = ceil(
                        conservative_vsize * splice_feerate_per_kw / 250.0
                    )
                    if selected[0].utxo.value < self.config.amount + conservative_fee:
                        if previous_outpoints == current_outpoints:
                            raise ValueError("Insufficient funds after fees.")
                        previous_outpoints = current_outpoints
                        selection_target = self.config.amount + conservative_fee
                        continue

                    break
                except ValueError as exc:
                    if str(exc) != "Insufficient funds after fees.":
                        raise

                    if previous_outpoints == current_outpoints:
                        raise

                    previous_outpoints = current_outpoints

                    input_types = [
                        "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected
                    ]

                    vsize = estimate_vsize(
                        input_types=input_types,
                        output_types=[
                            cln.funding_output_type,
                            "p2wpkh",
                        ],
                    )

                    estimated_fee = ceil(vsize * splice_feerate_per_kw / 250.0)

                    selection_target = self.config.amount + estimated_fee

            for warning in plan.warnings:
                logger.warning(warning)

            logger.info(
                "Splice-in amount: {} sats, fee: {} sats, change: {} sats",
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

            logger.info(
                "Locked {} UTXO for splice.",
                len(locked),
            )

            # --------------------------------------------------------
            # Start the CLN splice
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            try:
                result = recovery_journal.call(
                    recovery_id,
                    action="splice_init",
                    phase=LifecyclePhase.LOCKED.value,
                    fn=lambda: cln.splice_init(
                        channel_id=channel_id,
                        amount=plan.amount,
                        feerate_per_kw=splice_feerate_per_kw,
                    ),
                    locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                    owner_tokens=jmadapter._owner_tokens(),
                )
            except Exception as exc:
                # splice_init has no transaction id with which to identify
                # an accepted-but-unknown operation. Do not unlock inputs.
                lifecycle.release_locks = False
                raise SpliceRecoveryRequiredError(
                    "CLN splice_init outcome is unknown; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            returned_psbt = result.get("psbt")
            if not isinstance(returned_psbt, str) or not returned_psbt:
                lifecycle.release_locks = False
                raise SpliceRecoveryRequiredError(
                    "CLN splice_init returned an invalid PSBT; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            try:
                initial_psbt = base64.b64decode(
                    returned_psbt,
                    validate=True,
                )
            except (ValueError, binascii.Error) as exc:
                lifecycle.release_locks = False
                raise SpliceRecoveryRequiredError(
                    "CLN splice_init returned an invalid PSBT encoding; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            lifecycle.transition(LifecyclePhase.STARTED)
            lifecycle.release_locks = False

            # --------------------------------------------------------
            # Calculate the exact initiator fee for this splice
            # --------------------------------------------------------

            try:
                splice_fee, splice_weight = tx_builder.estimate_splice_fee(
                    psbt=initial_psbt,
                    feerate_per_kw=splice_feerate_per_kw,
                    add_change_output=self.config.amount != 0,
                )
            except (RuntimeError, ValueError) as exc:
                lifecycle.release_locks = False
                raise SpliceRecoveryRequiredError(
                    "Unable to calculate the splice fee from the CLN PSBT; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            splice_change = selected[0].utxo.value - plan.amount - splice_fee
            if splice_change < 0:
                lifecycle.release_locks = False
                raise SpliceRecoveryRequiredError(
                    "Selected JoinMarket UTXO cannot fund the CLN splice fee; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            splice_plan = replace(
                plan,
                fee=splice_fee,
                change=splice_change,
                vsize=(splice_weight + 3) // 4,
            )

            logger.info(
                "CLN splice weight: {} wu, required fee: {} sats",
                splice_weight,
                splice_fee,
            )

            # --------------------------------------------------------
            # Add the JoinMarket input
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            change_address = jmadapter.get_change_address(
                self.config.mixdepth,
            )
            previous_tx = await jmadapter.get_raw_transaction(
                selected[0].utxo.txid,
            )
            splice_psbt, splice_contribution = tx_builder.add_splice_in_input(
                psbt=initial_psbt,
                coin=selected[0],
                plan=splice_plan,
                change_address=change_address,
                wallet=jmadapter.require_wallet(),
                prev_tx=previous_tx,
            )
            tx_builder.validate_splice_psbt(
                psbt=splice_psbt,
                contribution=splice_contribution,
            )

            # --------------------------------------------------------
            # Negotiate the splice PSBT with CLN
            # --------------------------------------------------------

            commitments_secured = False

            while not commitments_secured:
                jmadapter.renew_locks(locked)
                try:
                    update_result = recovery_journal.call(
                        recovery_id,
                        action="splice_update",
                        phase=lifecycle.phase.value,
                        fn=lambda: cln.splice_update(
                            channel_id=channel_id,
                            psbt=splice_psbt,
                        ),
                        locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                        owner_tokens=jmadapter._owner_tokens(),
                        psbt=splice_psbt,
                    )
                except Exception as exc:
                    raise SpliceRecoveryRequiredError(
                        "CLN splice_update outcome is unknown; "
                        "JoinMarket UTXO remains locked for recovery",
                        channel_id=channel_id,
                        txid=None,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    ) from exc

                returned_psbt = update_result.get("psbt")
                if not isinstance(returned_psbt, str) or not returned_psbt:
                    raise SpliceRecoveryRequiredError(
                        "CLN splice_update returned an invalid PSBT; "
                        "JoinMarket UTXO remains locked for recovery",
                        channel_id=channel_id,
                        txid=None,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    )

                try:
                    splice_psbt = base64.b64decode(
                        returned_psbt,
                        validate=True,
                    )
                except (ValueError, binascii.Error) as exc:
                    raise SpliceRecoveryRequiredError(
                        "CLN splice_update returned an invalid PSBT encoding; "
                        "JoinMarket UTXO remains locked for recovery",
                        channel_id=channel_id,
                        txid=None,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    ) from exc

                try:
                    tx_builder.validate_splice_psbt(
                        psbt=splice_psbt,
                        contribution=splice_contribution,
                    )
                except Exception as exc:
                    raise SpliceRecoveryRequiredError(
                        "CLN splice_update returned a PSBT that violates the "
                        "negotiated splice economics; JoinMarket UTXO remains "
                        "locked for recovery",
                        channel_id=channel_id,
                        txid=None,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    ) from exc

                returned_commitments_secured = update_result.get("commitments_secured")
                if not isinstance(returned_commitments_secured, bool):
                    raise SpliceRecoveryRequiredError(
                        "CLN splice_update returned invalid commitments_secured; "
                        "JoinMarket UTXO remains locked for recovery",
                        channel_id=channel_id,
                        txid=None,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    )

                returned_signatures_secured = update_result.get("signatures_secured")
                if returned_signatures_secured is not None and not isinstance(
                    returned_signatures_secured,
                    bool,
                ):
                    raise SpliceRecoveryRequiredError(
                        "CLN splice_update returned invalid signatures_secured; "
                        "JoinMarket UTXO remains locked for recovery",
                        channel_id=channel_id,
                        txid=None,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    )

                commitments_secured = returned_commitments_secured
                lifecycle.transition(LifecyclePhase.UPDATED)

                if not commitments_secured:
                    lifecycle.transition(LifecyclePhase.STARTED)

            if confirm is not None and not confirm(
                channel_id,
                plan,
                splice_psbt,
            ):
                logger.info("Channel splice-in declined by user.")
                raise SpliceRecoveryRequiredError(
                    "Channel splice-in declined by user; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            # --------------------------------------------------------
            # Sign the JoinMarket input(s)
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            # Locate the approved JoinMarket input in the final negotiated
            # PSBT rather than relying on input ordering. The peer may add
            # inputs during interactive negotiation.
            try:
                jm_input_index = tx_builder.find_splice_input_index(
                    psbt=splice_psbt,
                    coin=plan.inputs[0],
                )
                signing_inputs = {jm_input_index: plan.inputs[0]}

                _signed_tx, _signed_txid, splice_psbt = tx_builder.sign_splice_psbt(
                    psbt=splice_psbt,
                    signing_inputs=signing_inputs,
                    wallet=jmadapter.require_wallet(),
                )
                txid = _signed_txid
                recovery_journal.update(
                    recovery_id,
                    txid=txid,
                    psbt=base64.b64encode(splice_psbt).decode("ascii"),
                )
                tx_builder.validate_splice_psbt(
                    psbt=splice_psbt,
                    contribution=splice_contribution,
                )
            except Exception as exc:
                raise SpliceRecoveryRequiredError(
                    "Unable to sign the splice PSBT; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            # --------------------------------------------------------
            # Submit the final signed splice to CLN
            # --------------------------------------------------------

            jmadapter.renew_locks(locked)

            try:
                signed_result = recovery_journal.call(
                    recovery_id,
                    action="splice_signed",
                    phase=LifecyclePhase.UPDATED.value,
                    fn=lambda: cln.splice_signed(
                        channel_id=channel_id,
                        psbt=splice_psbt,
                    ),
                    locked_outpoints=[(c.utxo.txid, c.utxo.vout) for c in locked],
                    owner_tokens=jmadapter._owner_tokens(),
                    psbt=splice_psbt,
                )
            except Exception as exc:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed outcome is unknown; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            returned_psbt = signed_result.get("psbt")
            if not isinstance(returned_psbt, str) or not returned_psbt:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed returned an invalid PSBT; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            returned_tx = signed_result.get("tx")
            if not isinstance(returned_tx, str) or not returned_tx:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed returned an invalid transaction; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            returned_txid = signed_result.get("txid")
            if not isinstance(returned_txid, str) or not returned_txid:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed returned an invalid transaction id; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            txid = returned_txid

            try:
                base64.b64decode(
                    returned_psbt,
                    validate=True,
                )
            except (ValueError, binascii.Error) as exc:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed returned an invalid PSBT encoding; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=txid,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            try:
                signed_tx = bytes.fromhex(returned_tx)
            except ValueError as exc:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed returned an invalid transaction encoding; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=txid,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            if not signed_tx:
                raise SpliceRecoveryRequiredError(
                    "CLN splice_signed returned an empty transaction; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=txid,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                )

            lifecycle.transition(LifecyclePhase.SIGNED)
            lifecycle.release_locks = False

            logger.info(
                "CLN accepted splice transaction {}.",
                txid,
            )

        except SpliceRecoveryRequiredError as exc:
            operation_error = exc
            raise
        except Exception as exc:
            operation_error = exc
            logger.error(
                "Failed to execute channel splice: {}",
                exc,
            )
            raise
        finally:
            await lifecycle.cleanup(
                locked=locked,
                adapter=jmadapter,
                close_message="Failed to close JoinMarket wallet after splice",
                unlock_message="Failed to unlock",
            )

            if lifecycle.cleanup_errors and operation_error is None:
                logger.warning(
                    "Splice completed successfully, but JoinMarket cleanup failed",
                )

            lifecycle.resolve_if_clean(
                recovery_journal,
                recovery_id,
                terminal_phase=LifecyclePhase.SIGNED,
            )

            lifecycle.raise_recovery_if_needed(
                operation_error,
                SpliceRecoveryRequiredError,
                lambda error: SpliceRecoveryRequiredError(
                    "Channel splice failed and cleanup also failed; "
                    "manual recovery is required",
                    channel_id=channel_id,
                    txid=txid,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ),
            )

        if txid is None:
            raise RuntimeError("Splice completed without a transaction id")
        return txid


def confirm_splice_in(
    channel_id: str,
    plan: ExecutionPlan,
    psbt: bytes,
) -> bool:
    """Confirm a negotiated splice-in before submitting it to CLN."""
    typer.echo("")
    typer.echo("Channel splice-in")
    typer.echo("=================")
    typer.echo(f"Channel ID:       {channel_id}")
    typer.echo(f"Splice-in amount:  {plan.amount:,} sats")
    typer.echo(f"Fee:               {plan.fee:,} sats")
    typer.echo(f"Virtual size:      {plan.vsize} vbytes")
    typer.echo(f"Change:            {plan.change:,} sats")
    typer.echo("")

    typer.echo("JoinMarket inputs:")
    for coin in plan.inputs:
        typer.echo(
            f"  {coin.utxo.txid}:{coin.utxo.vout} "
            f"{coin.utxo.value:,} sats "
            f"({coin.status})"
        )

    if plan.warnings:
        typer.echo("")
        typer.echo("Warnings:")
        for warning in plan.warnings:
            typer.echo(f"  WARNING: {warning}")

    typer.echo("")
    typer.echo(
        f"Negotiated PSBT: {len(psbt)} bytes",
    )
    typer.echo("")

    return typer.confirm(
        "Proceed with channel splice-in?",
        default=False,
    )
