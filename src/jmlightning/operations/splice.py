from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from enum import StrEnum, auto
from math import ceil
from pathlib import Path

import typer
from jmcore.bitcoin import estimate_vsize
from loguru import logger

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.cln import CLNBackend
from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.policy import Capability, PolicyEngine
from jmlightning.tx_builder import TxBuilder

SpliceConfirmationCallback = Callable[
    [str, ExecutionPlan, bytes],
    bool,
]


class SplicePhase(StrEnum):
    PRESTART = auto()
    LOCKED = auto()
    STARTED = auto()
    UPDATED = auto()
    SIGNED = auto()


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
    ) -> None:
        policy = PolicyEngine()
        planner = Planner()
        jmadapter = JoinMarketAdapter(config=self.config)
        cln = CLNBackend(str(self.cln_socket))
        tx_builder = TxBuilder()

        selected: list[ClassifiedUTXO] = []
        locked: list[ClassifiedUTXO] = []
        phase = SplicePhase.PRESTART
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
                        "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected
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

            phase = SplicePhase.LOCKED

            logger.info(
                "Locked {} UTXO for splice.",
                len(locked),
            )

            # --------------------------------------------------------
            # Start the CLN splice
            # --------------------------------------------------------

            try:
                result = cln.splice_init(
                    channel_id=channel_id,
                    amount=plan.amount,
                )
            except Exception as exc:
                # splice_init has no transaction id with which to identify
                # an accepted-but-unknown operation. Do not unlock inputs.
                release_locks = False
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
                release_locks = False
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
                release_locks = False
                raise SpliceRecoveryRequiredError(
                    "CLN splice_init returned an invalid PSBT encoding; "
                    "JoinMarket UTXO remains locked for recovery",
                    channel_id=channel_id,
                    txid=None,
                    locked_outpoints=tuple(
                        (coin.utxo.txid, coin.utxo.vout) for coin in locked
                    ),
                ) from exc

            phase = SplicePhase.STARTED
            release_locks = False

            # --------------------------------------------------------
            # Add the JoinMarket input
            # --------------------------------------------------------

            change_address = jmadapter.get_change_address(
                self.config.mixdepth,
            )
            splice_psbt = tx_builder.add_splice_in_input(
                psbt=initial_psbt,
                coin=selected[0],
                plan=plan,
                change_address=change_address,
                wallet=jmadapter.require_wallet(),
            )

            # --------------------------------------------------------
            # Negotiate the splice PSBT with CLN
            # --------------------------------------------------------

            commitments_secured = False

            while not commitments_secured:
                try:
                    update_result = cln.splice_update(
                        channel_id=channel_id,
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
                phase = SplicePhase.UPDATED

                if not commitments_secured:
                    phase = SplicePhase.STARTED

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
            # Submit the final signed splice to CLN
            # --------------------------------------------------------

            try:
                signed_result = cln.splice_signed(
                    channel_id=channel_id,
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

            phase = SplicePhase.SIGNED
            release_locks = False

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
            # --------------------------------------------------------
            # Cleanup
            # --------------------------------------------------------

            if release_locks:
                for coin in locked:
                    try:
                        jmadapter.unlock(coin)
                    except Exception as exc:
                        cleanup_errors.append(exc)
                        logger.error(
                            "Failed to unlock {}:{} after splice phase {}: {}",
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
                    "Failed to close JoinMarket wallet after splice phase {}: {}",
                    phase,
                    exc,
                )

            if cleanup_errors and operation_error is None:
                logger.warning(
                    "Splice completed successfully, but JoinMarket cleanup failed",
                )

            if cleanup_errors and operation_error is not None:
                logger.error(
                    "Channel splice failed and cleanup also failed; "
                    "manual recovery is required",
                )
                if not isinstance(operation_error, SpliceRecoveryRequiredError):
                    raise SpliceRecoveryRequiredError(
                        "Channel splice failed and cleanup also failed; "
                        "manual recovery is required",
                        channel_id=channel_id,
                        txid=txid,
                        locked_outpoints=tuple(
                            (coin.utxo.txid, coin.utxo.vout) for coin in locked
                        ),
                    ) from operation_error


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
