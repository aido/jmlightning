from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import StrEnum, auto
from math import ceil
from pathlib import Path

from jmcore.bitcoin import (
    ParsedTransaction,
    address_to_scriptpubkey,
    estimate_vsize,
    get_address_type,
    psbt_to_base64,
    serialize_transaction,
)
from jmcore.constants import MAX_MONEY
from loguru import logger
from pyln.client import LightningRpc

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.cln import CLNBackend
from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import ExecutionPlan, Planner
from jmlightning.policy import Capability, PolicyEngine
from jmlightning.tx_builder import TxBuilder


class PeerSwapPhase(StrEnum):
    """Lifecycle state for a prepared PeerSwap transaction."""

    PREPARED = auto()
    BROADCAST = auto()
    DISCARDED = auto()


@dataclass(frozen=True, slots=True)
class PeerSwapOutput:
    """One output requested by PeerSwap through CLN ``txprepare``."""

    address: str
    amount: int


@dataclass(frozen=True, slots=True)
class PeerSwapPrepareTxRequest:
    """Validated parameters from a PeerSwap ``txprepare`` RPC request."""

    outputs: tuple[PeerSwapOutput, ...]
    feerate: str | int | None
    minconf: int
    utxos: tuple[str, ...]

    @classmethod
    def from_rpc(cls, params: object) -> PeerSwapPrepareTxRequest:
        if not isinstance(params, dict):
            raise ValueError("txprepare params must be an object")

        allowed_params = {"outputs", "feerate", "minconf", "utxos"}
        unknown_params = set(params) - allowed_params
        if unknown_params:
            raise ValueError(
                "txprepare contains unsupported parameter(s): "
                + ", ".join(sorted(unknown_params))
            )

        raw_outputs = params.get("outputs")
        if not isinstance(raw_outputs, list) or not raw_outputs:
            raise ValueError("txprepare requires at least one output")

        outputs: list[PeerSwapOutput] = []
        for index, output in enumerate(raw_outputs):
            if not isinstance(output, dict) or len(output) != 1:
                raise ValueError(f"Invalid txprepare output {index}")

            address, amount = next(iter(output.items()))
            if not isinstance(address, str) or not address:
                raise ValueError(f"Invalid txprepare output address {index}")
            if address == "all":
                raise ValueError("PeerSwap txprepare does not support an 'all' output")
            if isinstance(amount, str) and amount.endswith("sat"):
                satoshi = amount[:-3]
                if not satoshi.isdigit():
                    raise ValueError(f"Invalid txprepare output amount {index}")
                amount = int(satoshi)
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or not 0 < amount <= MAX_MONEY
            ):
                raise ValueError(f"Invalid txprepare output amount {index}")

            # Validate the destination before any wallet state is touched.
            # TxBuilder performs the same conversion later, but rejecting a
            # malformed address at the RPC boundary avoids carrying an invalid
            # request into planning or transaction preparation.
            try:
                address_to_scriptpubkey(address)
            except Exception as exc:
                raise ValueError(f"Invalid txprepare output address {index}") from exc

            outputs.append(PeerSwapOutput(address=address, amount=amount))

        feerate = params.get("feerate")
        if feerate is not None and not isinstance(feerate, (str, int)):
            raise ValueError("txprepare feerate must be a string or integer")
        if isinstance(feerate, bool):
            raise ValueError("txprepare feerate must be a string or integer")

        minconf = params.get("minconf", 1)
        # CLN defines ``minconf`` as a u32. Keep the RPC boundary aligned
        # with that contract before any wallet state is queried.
        if (
            isinstance(minconf, bool)
            or not isinstance(minconf, int)
            or not 0 <= minconf <= 0xFFFFFFFF
        ):
            raise ValueError("txprepare minconf must be a uint32")

        raw_utxos = params.get("utxos", [])
        if not isinstance(raw_utxos, list) or not all(
            isinstance(utxo, str) and utxo for utxo in raw_utxos
        ):
            raise ValueError("txprepare utxos must be a list of outpoints")

        # Validate explicit outpoints before any wallet state is touched.
        # PeerSwap passes CLN outpoints as ``<txid>:<vout>``.
        seen_utxos: set[str] = set()
        canonical_utxos: list[str] = []
        for utxo in raw_utxos:
            txid, separator, vout = utxo.partition(":")
            if (
                separator != ":"
                or len(txid) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in txid)
                or not vout.isdigit()
                or int(vout) > 0xFFFFFFFF
            ):
                raise ValueError(f"Invalid txprepare UTXO outpoint: {utxo}")

            # Outpoint txids are hex values, so canonicalise their case before
            # duplicate detection and wallet selection. This keeps explicit
            # UTXOs consistent with the canonical transaction-id representation.
            canonical = f"{txid.lower()}:{vout}"
            if canonical in seen_utxos:
                raise ValueError(f"Duplicate txprepare UTXO outpoint: {utxo}")
            seen_utxos.add(canonical)
            canonical_utxos.append(canonical)

        return cls(
            outputs=tuple(outputs),
            feerate=feerate,
            minconf=minconf,
            utxos=tuple(canonical_utxos),
        )


@dataclass(slots=True)
class PreparedPeerSwapTransaction:
    """A prepared PeerSwap transaction and the JoinMarket state it owns."""

    tx: ParsedTransaction
    txid: str
    locked: list[ClassifiedUTXO]
    adapter: JoinMarketAdapter
    psbt: bytes
    # Explicitly retain the reservation set as part of the prepared operation
    # state. ``locked`` remains as a compatibility alias for callers/tests
    # which construct this value directly. The reservation set is what the
    # long-lived lifecycle methods renew and release.
    reservations: tuple[ClassifiedUTXO, ...] = ()
    phase: PeerSwapPhase = PeerSwapPhase.PREPARED
    released_reservations: set[tuple[str, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.reservations:
            self.reservations = tuple(self.locked)
        elif not self.locked:
            self.locked = list(self.reservations)

    def renew_reservations(self) -> None:
        """Renew the reservations explicitly owned by this operation."""
        self.require_phase(PeerSwapPhase.PREPARED)
        self.adapter.renew_locks(list(self.reservations))

    def require_phase(self, expected: PeerSwapPhase) -> None:
        """Require this prepared transaction to be in ``expected`` state."""
        if self.phase is not expected:
            raise ValueError(
                f"PeerSwap transaction {self.txid} is in {self.phase} state"
            )

    def transition(self, expected: PeerSwapPhase, target: PeerSwapPhase) -> None:
        """Move this prepared transaction between explicitly allowed states."""
        self.require_phase(expected)
        self.phase = target


class PeerSwapPrepareTxOperation:
    """Prepare a PeerSwap transaction using JoinMarket UTXOs."""

    required_capability = Capability.SWAP

    def __init__(self, config: CLNConfig, cln_socket: Path) -> None:
        self.config = config
        self.cln_socket = cln_socket
        self._prepared: dict[str, PreparedPeerSwapTransaction] = {}
        self._connected_adapter: JoinMarketAdapter | None = None

    async def connect(self) -> None:
        """Pre-connect the wallet before serving a PeerSwap transaction request."""
        if self._connected_adapter is not None:
            return

        adapter = JoinMarketAdapter(config=self.config)
        try:
            await adapter.connect()
        except Exception:
            await adapter.close()
            raise
        self._connected_adapter = adapter

    async def execute(
        self,
        request: PeerSwapPrepareTxRequest,
    ) -> PreparedPeerSwapTransaction:
        """Build and sign the transaction requested by PeerSwap."""
        logger.info(
            "PeerSwap JM operation txprepare start socket={} "
            "outputs={} minconf={} utxos={}",
            self.cln_socket,
            len(request.outputs),
            request.minconf,
            len(request.utxos),
        )
        policy = PolicyEngine()
        planner = Planner()
        jmadapter = self._connected_adapter
        if jmadapter is None:
            jmadapter = JoinMarketAdapter(config=self.config)
            await jmadapter.connect()
        self._connected_adapter = None
        cln = CLNBackend(str(self.cln_socket))
        tx_builder = TxBuilder()

        try:
            logger.info("PeerSwap JM operation connected socket={}", self.cln_socket)
            available = jmadapter.get_utxos(mixdepth=self.config.mixdepth)
            logger.info(
                "PeerSwap JM operation wallet UTXOs socket={} available={}",
                self.cln_socket,
                len(available),
            )
            allowed = [
                coin
                for coin in policy.filter(available, self.required_capability)
                if coin.utxo.confirmations >= request.minconf
            ]

            if not allowed:
                raise RuntimeError(
                    f"No UTXOs in mixdepth {self.config.mixdepth} "
                    f"are permitted for {self.required_capability.name}"
                )

            logger.info(
                "PeerSwap JM operation querying CLN feerate socket={} requested={}",
                self.cln_socket,
                request.feerate,
            )
            fee_rate = cln.get_fee_rate(feerate=request.feerate)
            mempool_min_fee = await jmadapter.get_mempool_min_fee()
            if mempool_min_fee is not None and fee_rate < mempool_min_fee:
                logger.info(
                    "PeerSwap JM operation raised feerate to Bitcoin mempool "
                    "minimum socket={} requested_rate={} mempool_min_rate={}",
                    self.cln_socket,
                    fee_rate,
                    mempool_min_fee,
                )
                fee_rate = mempool_min_fee
            logger.info(
                "PeerSwap JM operation got effective feerate socket={} rate={}",
                self.cln_socket,
                fee_rate,
            )
            selected = self._select_utxos(
                request=request,
                allowed=allowed,
                adapter=jmadapter,
                fee_rate=fee_rate,
                planner=planner,
            )
            plan = self._build_plan(planner, selected, request, fee_rate)
            logger.info(
                "PeerSwap JM operation selected UTXOs socket={} "
                "selected={} total_sat={}",
                self.cln_socket,
                len(selected),
                sum(coin.utxo.value for coin in selected),
            )

            locked: list[ClassifiedUTXO] = []
            try:
                for coin in selected:
                    jmadapter.lock(coin)
                    locked.append(coin)

                # Prepared PeerSwap transactions may remain idle for an
                # arbitrary period. Keep their JoinMarket reservations alive
                # for as long as the prepared transaction is retained.
                jmadapter.start_lock_renewal()

                change_address = jmadapter.get_change_address(self.config.mixdepth)
                tx, txid, psbt = tx_builder.build_and_sign_multifunding_tx(
                    plan=plan,
                    funding_addresses=[output.address for output in request.outputs],
                    change_address=change_address,
                    wallet=jmadapter.require_wallet(),
                    finalise_psbt=True,
                )
                self._validate_txid(txid, "JoinMarket transaction id")
                logger.info(
                    "PeerSwap JM operation built transaction socket={} txid={}",
                    self.cln_socket,
                    txid,
                )
                # Keep transaction ids canonical internally so case-insensitive
                # hex input cannot create duplicate prepared-state identities.
                txid = txid.lower()
            except asyncio.CancelledError:
                for coin in reversed(locked):
                    try:
                        jmadapter.unlock(coin)
                    except Exception:
                        pass
                raise
            except Exception:
                for coin in reversed(locked):
                    try:
                        jmadapter.unlock(coin)
                    except Exception:
                        pass
                raise

            if txid in self._prepared:
                for coin in reversed(locked):
                    jmadapter.unlock(coin)
                raise RuntimeError(f"PeerSwap transaction {txid} is already prepared")

            prepared = PreparedPeerSwapTransaction(
                tx=tx,
                txid=txid,
                locked=locked,
                adapter=jmadapter,
                psbt=psbt,
                reservations=tuple(locked),
            )
            self._prepared[txid] = prepared
            logger.info(
                "PeerSwap JM operation txprepare complete socket={} txid={} inputs={}",
                self.cln_socket,
                txid,
                len(selected),
            )
            return prepared
        except asyncio.CancelledError:
            logger.info(
                "PeerSwap JM operation txprepare cancelled socket={}",
                self.cln_socket,
            )
            await jmadapter.close()
            raise
        except Exception as exc:
            logger.exception(
                "PeerSwap JM operation txprepare failed socket={}: {}",
                self.cln_socket,
                exc,
            )
            await jmadapter.close()
            raise

    async def prepared_result(self, txid: str) -> dict[str, str]:
        """Return the CLN ``txprepare`` response for a prepared transaction."""
        prepared = self._prepared.get(txid)
        if prepared is None:
            raise ValueError(f"PeerSwap transaction {txid} is not prepared")
        if prepared.phase is not PeerSwapPhase.PREPARED:
            raise ValueError(
                f"PeerSwap transaction {txid} is in {prepared.phase} state"
            )

        # The prepared transaction owns its reservation set explicitly. Renew
        # it at the operation boundary so a stale lease can never be used.
        prepared.renew_reservations()

        return self._prepare_result(prepared)

    async def send(self, txid: str) -> dict[str, str]:
        """Broadcast a prepared PeerSwap transaction and release its inputs."""
        prepared = self._prepared.get(txid)
        if prepared is None:
            raise ValueError(f"PeerSwap transaction {txid} is not prepared")
        prepared.require_phase(PeerSwapPhase.PREPARED)

        # Renew immediately before broadcast. If the prepared transaction was
        # left idle beyond the lease lifetime, do not broadcast inputs that
        # are no longer reserved by this operation.
        prepared.renew_reservations()

        logger.info(
            "PeerSwap JM operation txsend start socket={} txid={}",
            self.cln_socket,
            txid,
        )
        # A broadcast is an externally visible side effect. If the rendezvous
        # cancellation races with this await, cancelling the coroutine must not
        # make us guess whether the transaction was broadcast. Let the backend
        # call finish before the operation is allowed to clean up its prepared
        # state.
        broadcast_task = asyncio.create_task(prepared.adapter.broadcast(prepared.tx))
        try:
            broadcast_txid = await asyncio.shield(broadcast_task)
        except asyncio.CancelledError:
            broadcast_txid = await broadcast_task
        self._validate_txid(broadcast_txid, "JoinMarket broadcast transaction id")
        broadcast_txid = broadcast_txid.lower()
        if broadcast_txid != txid:
            raise RuntimeError(
                "JoinMarket broadcast returned an unexpected transaction id: "
                f"expected {txid}, got {broadcast_txid}"
            )

        # Broadcasting is the terminal transaction state. Keep the state
        # retained until JoinMarket cleanup succeeds so a failed unlock/close
        # cannot strand an input until its lease expires. The BROADCAST phase
        # also prevents a retry from broadcasting the transaction twice.
        prepared.transition(PeerSwapPhase.PREPARED, PeerSwapPhase.BROADCAST)
        try:
            await self._cleanup_prepared(prepared)
        except RuntimeError as exc:
            logger.warning(
                "PeerSwap transaction {} was broadcast but cleanup failed; "
                "retaining state for retry: {}",
                txid,
                exc,
            )
        else:
            self._prepared.pop(txid, None)

        logger.info(
            "PeerSwap JM operation txsend complete socket={} txid={}",
            self.cln_socket,
            txid,
        )
        return self._send_result(prepared)

    @staticmethod
    def _validate_txid(txid: str, field: str = "transaction id") -> None:
        """Require a transaction id to be exactly one 32-byte hex value."""
        if not isinstance(txid, str) or len(txid) != 64:
            raise ValueError(f"{field} must be a 64-character hexadecimal value")
        try:
            bytes.fromhex(txid)
        except ValueError as exc:
            raise ValueError(
                f"{field} must be a 64-character hexadecimal value"
            ) from exc

    @staticmethod
    def _unsigned_tx(prepared: PreparedPeerSwapTransaction) -> str:
        # CLN txprepare returns the transaction without its witness data.
        return serialize_transaction(
            prepared.tx.version,
            prepared.tx.inputs,
            prepared.tx.outputs,
            prepared.tx.locktime,
        ).hex()

    @staticmethod
    def _signed_tx(prepared: PreparedPeerSwapTransaction) -> str:
        return serialize_transaction(
            prepared.tx.version,
            prepared.tx.inputs,
            prepared.tx.outputs,
            prepared.tx.locktime,
            prepared.tx.witnesses,
        ).hex()

    @classmethod
    def _prepare_result(cls, prepared: PreparedPeerSwapTransaction) -> dict[str, str]:
        # CLN txprepare returns the unsigned transaction, its txid and PSBT.
        return {
            "unsigned_tx": cls._unsigned_tx(prepared),
            "txid": prepared.txid,
            "psbt": psbt_to_base64(prepared.psbt),
        }

    @classmethod
    def _send_result(cls, prepared: PreparedPeerSwapTransaction) -> dict[str, str]:
        # CLN txsend returns the fully signed transaction, its txid and PSBT.
        return {
            "tx": cls._signed_tx(prepared),
            "txid": prepared.txid,
            "psbt": psbt_to_base64(prepared.psbt),
        }

    async def _cleanup_prepared(self, prepared: PreparedPeerSwapTransaction) -> None:
        """Release a prepared transaction's JoinMarket resources.

        The adapter remains open when an unlock fails because its owner token
        and wallet handle are required for a safe retry. Likewise, state is
        only removed by the caller after both unlocking and adapter close have
        succeeded.
        """
        cleanup_errors: list[Exception] = []
        for coin in reversed(prepared.reservations):
            outpoint = (coin.utxo.txid, coin.utxo.vout)
            if outpoint in prepared.released_reservations:
                continue
            try:
                prepared.adapter.unlock(coin)
            except Exception as exc:
                cleanup_errors.append(exc)
                logger.error(
                    "Failed to unlock {}:{} while cleaning up PeerSwap "
                    "transaction {}: {}",
                    coin.utxo.txid,
                    coin.utxo.vout,
                    prepared.txid,
                    exc,
                )
            else:
                prepared.released_reservations.add(outpoint)

        # Do not close the adapter while any release failed: closing the wallet
        # would discard the local owner token needed for a later compare-and-
        # release retry.
        if cleanup_errors:
            raise RuntimeError(
                f"Failed to fully clean up PeerSwap transaction {prepared.txid} "
                f"({len(cleanup_errors)} error(s))"
            ) from cleanup_errors[0]

        try:
            await prepared.adapter.close()
        except Exception as exc:
            logger.error(
                "Failed to close PeerSwap JoinMarket adapter while cleaning up "
                "transaction {}: {}",
                prepared.txid,
                exc,
            )
            raise RuntimeError(
                f"Failed to fully clean up PeerSwap transaction {prepared.txid} "
                "(1 error(s))"
            ) from exc

    async def close(self) -> None:
        """Release all prepared transactions and any pre-connected wallet."""
        for txid, prepared in list(self._prepared.items()):
            if prepared.phase not in {PeerSwapPhase.PREPARED, PeerSwapPhase.BROADCAST}:
                continue
            try:
                await self._cleanup_prepared(prepared)
            except Exception as exc:
                logger.error(
                    "Failed to clean up PeerSwap transaction {}: {}",
                    txid,
                    exc,
                )
                continue
            self._prepared.pop(txid, None)
            if prepared.phase is PeerSwapPhase.PREPARED:
                prepared.transition(PeerSwapPhase.PREPARED, PeerSwapPhase.DISCARDED)

        adapter = self._connected_adapter
        self._connected_adapter = None
        if adapter is not None:
            await adapter.close()

    async def discard(self, txid: str) -> dict[str, str]:
        """Release a prepared transaction and its JoinMarket UTXOs."""
        prepared = self._prepared.get(txid)
        if prepared is None:
            raise ValueError(f"PeerSwap transaction {txid} is not prepared")
        if prepared.phase not in {PeerSwapPhase.PREPARED, PeerSwapPhase.BROADCAST}:
            raise ValueError(
                f"PeerSwap transaction {txid} is in {prepared.phase} state"
            )

        # Keep state until every cleanup step succeeds. A BROADCAST transaction
        # may reach this path when txsend completed but resource cleanup failed;
        # retrying txdiscard must only release resources and must never broadcast
        # the transaction again.
        phase = prepared.phase
        await self._cleanup_prepared(prepared)
        if phase is PeerSwapPhase.PREPARED:
            prepared.transition(PeerSwapPhase.PREPARED, PeerSwapPhase.DISCARDED)
        self._prepared.pop(txid, None)

        return {
            "unsigned_tx": self._unsigned_tx(prepared),
            "txid": prepared.txid,
        }

    def _select_utxos(
        self,
        request: PeerSwapPrepareTxRequest,
        allowed: list[ClassifiedUTXO],
        adapter: JoinMarketAdapter,
        fee_rate: float,
        planner: Planner,
    ) -> list[ClassifiedUTXO]:
        by_outpoint = {f"{coin.utxo.txid}:{coin.utxo.vout}": coin for coin in allowed}
        if request.utxos:
            selected: list[ClassifiedUTXO] = []
            for outpoint in request.utxos:
                coin = by_outpoint.get(outpoint)
                if coin is None:
                    raise ValueError(
                        f"UTXO {outpoint} is not available for PeerSwap funding"
                    )
                selected.append(coin)
            return selected

        allowed_outpoints = {(coin.utxo.txid, coin.utxo.vout) for coin in allowed}
        target_amount = sum(output.amount for output in request.outputs)
        output_types = [self._output_type(output.address) for output in request.outputs]
        previous_outpoints: set[tuple[str, int]] | None = None
        selection_target = target_amount + ceil(
            estimate_vsize(
                input_types=["p2wpkh"],
                output_types=[*output_types, "p2wpkh"],
            )
            * fee_rate
        )

        while True:
            selected_raw = adapter.select_utxos(
                mixdepth=self.config.mixdepth,
                target_amount=selection_target,
                allowed_outpoints=allowed_outpoints,
            )
            try:
                selected = [
                    by_outpoint[f"{utxo.txid}:{utxo.vout}"] for utxo in selected_raw
                ]
            except KeyError as exc:
                raise RuntimeError(
                    "JoinMarket selected a UTXO that was not present in the "
                    "policy-approved selection pool"
                ) from exc

            if not selected:
                raise RuntimeError("Unable to select UTXOs for SWAP")

            try:
                planner.build_multi_plan(
                    selected_coins=selected,
                    target_amounts=[output.amount for output in request.outputs],
                    fee_rate=fee_rate,
                    funding_output_types=output_types,
                )
                return selected
            except ValueError as exc:
                if str(exc) != "Insufficient funds after fees.":
                    raise

                current_outpoints = {
                    (coin.utxo.txid, coin.utxo.vout) for coin in selected
                }
                if previous_outpoints == current_outpoints:
                    raise
                previous_outpoints = current_outpoints
                input_types = [
                    "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected
                ]
                vsize = estimate_vsize(
                    input_types=input_types,
                    output_types=[*output_types, "p2wpkh"],
                )
                selection_target = target_amount + ceil(vsize * fee_rate)

    @staticmethod
    def _build_plan(
        planner: Planner,
        selected: list[ClassifiedUTXO],
        request: PeerSwapPrepareTxRequest,
        fee_rate: float,
    ) -> ExecutionPlan:
        return planner.build_multi_plan(
            selected_coins=selected,
            target_amounts=[output.amount for output in request.outputs],
            fee_rate=fee_rate,
            funding_output_types=[
                PeerSwapPrepareTxOperation._output_type(output.address)
                for output in request.outputs
            ],
        )

    @staticmethod
    def _output_type(address: str) -> str:
        return get_address_type(address)


class PeerSwapRendezvousClient:
    """Maintain CLN RPC waiters for requests arriving from jm-peerswap."""

    def __init__(
        self,
        cln_socket: Path,
        handler: Callable[[str, object], object],
        pool_size: int = 4,
        rpc_factory: Callable[[str], LightningRpc] = LightningRpc,
    ) -> None:
        if pool_size <= 0:
            raise ValueError("pool_size must be positive")
        self.cln_socket = cln_socket
        self.handler = handler
        self.pool_size = pool_size
        self.rpc_factory = rpc_factory
        self._stopping = threading.Event()
        self._dispatch_condition = threading.Condition()
        self._active_dispatches = 0
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("PeerSwap rendezvous client is already running")

        self._stopping.clear()
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"jm-lightning-peerswap-{index}",
                daemon=True,
            )
            for index in range(self.pool_size)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._dispatch_condition:
            while self._active_dispatches:
                self._dispatch_condition.wait()
        threads = self._threads
        self._threads = []
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        close = getattr(self.handler, "close", None)
        if callable(close):
            close()

    def _worker(self) -> None:
        rpc = self.rpc_factory(str(self.cln_socket))
        while not self._stopping.is_set():
            try:
                request = rpc.call("jmpeerswap-request", {})
                with self._dispatch_condition:
                    if self._stopping.is_set():
                        return
                    self._active_dispatches += 1
                logger.info(
                    "PeerSwap rendezvous client socket={} "
                    "received method={} request_id={}",
                    self.cln_socket,
                    request.get("method") if isinstance(request, dict) else "?",
                    request.get("request_id") if isinstance(request, dict) else "?",
                )
                try:
                    self._handle_request(rpc, request)
                finally:
                    with self._dispatch_condition:
                        self._active_dispatches -= 1
                        if self._active_dispatches == 0:
                            self._dispatch_condition.notify_all()
            except Exception as exc:
                logger.exception(
                    "PeerSwap rendezvous client socket={} failed: {}",
                    self.cln_socket,
                    exc,
                )
                if self._stopping.is_set():
                    return
                continue

    def _handle_request(self, rpc: LightningRpc, request: object) -> None:
        if not isinstance(request, dict):
            return

        request_id = request.get("request_id")
        method = request.get("method")
        if not isinstance(request_id, str) or not request_id:
            return
        if method not in {"txprepare", "txsend", "txdiscard"}:
            self._send_error(rpc, request_id, -32601, "Unsupported PeerSwap request")
            return

        logger.info(
            "PeerSwap rendezvous dispatch socket={} method={} request_id={}",
            self.cln_socket,
            method,
            request_id,
        )

        start_request = getattr(self.handler, "start_request", None)
        cancel_request = getattr(self.handler, "cancel_request", None)
        if callable(start_request) and callable(cancel_request):
            self._handle_cancellable_request(
                rpc,
                request_id,
                method,
                request.get("params", {}),
                start_request,
                cancel_request,
            )
            return

        try:
            result = self.handler(method, request.get("params", {}))
        except ValueError as exc:
            self._send_error(rpc, request_id, -32602, str(exc))
            return
        except Exception as exc:
            self._send_error(rpc, request_id, -32603, str(exc))
            return

        logger.info(
            "PeerSwap rendezvous response socket={} method={} request_id={} ok",
            self.cln_socket,
            method,
            request_id,
        )
        rpc.call(
            "jmpeerswap-response",
            {"request_id": request_id, "result": result},
        )

    def _handle_cancellable_request(
        self,
        rpc: LightningRpc,
        request_id: str,
        method: str,
        params: object,
        start_request: Callable[[str, str, object], Future[object]],
        cancel_request: Callable[[str], None],
    ) -> None:
        cancelled = threading.Event()
        request_finished = threading.Event()
        future = start_request(request_id, method, params)
        if not isinstance(future, Future):
            raise TypeError("PeerSwap cancellable handler returned an invalid future")

        def watch_cancel() -> None:
            try:
                cancel_rpc = self.rpc_factory(str(self.cln_socket))
                result = cancel_rpc.call(
                    "jmpeerswap-cancel",
                    {"request_id": request_id},
                )
                if (
                    isinstance(result, dict)
                    and result.get("request_id") == request_id
                    and result.get("state") in {"cancelled", "timed_out"}
                ):
                    with self._dispatch_condition:
                        if request_finished.is_set():
                            return
                        cancelled.set()
                        cancel_request(request_id)
            except Exception as exc:
                if not self._stopping.is_set():
                    logger.exception(
                        "PeerSwap cancellation watcher socket={} "
                        "request_id={} failed: {}",
                        self.cln_socket,
                        request_id,
                        exc,
                    )

        watcher = threading.Thread(
            target=watch_cancel,
            name=f"jm-lightning-peerswap-cancel-{request_id[:8]}",
            daemon=True,
        )
        watcher.start()

        try:
            result = future.result()
        except Exception as exc:
            if cancelled.is_set():
                logger.info(
                    "PeerSwap rendezvous operation cancelled socket={} request_id={}",
                    self.cln_socket,
                    request_id,
                )
                finish_request = getattr(self.handler, "finish_request", None)
                if callable(finish_request):
                    finish_request(request_id)
                request_finished.set()
                return
            if isinstance(exc, ValueError):
                self._send_error(rpc, request_id, -32602, str(exc))
            else:
                self._send_error(rpc, request_id, -32603, str(exc))
            finish_request = getattr(self.handler, "finish_request", None)
            if callable(finish_request):
                finish_request(request_id)
            request_finished.set()
            return

        if cancelled.is_set():
            logger.info(
                "PeerSwap rendezvous operation completed after cancellation socket={} "
                "request_id={}",
                self.cln_socket,
                request_id,
            )
            cancel_request(request_id)
            finish_request = getattr(self.handler, "finish_request", None)
            if callable(finish_request):
                finish_request(request_id)
            return

        logger.info(
            "PeerSwap rendezvous response socket={} method={} request_id={} ok",
            self.cln_socket,
            method,
            request_id,
        )
        try:
            rpc.call(
                "jmpeerswap-response",
                {"request_id": request_id, "result": result},
            )
        except Exception:
            # The PeerSwap side may have disconnected between operation
            # completion and response delivery. If this was txprepare, the
            # completed result still owns a prepared JoinMarket transaction,
            # so cancellation must discard it before the dispatcher releases
            # the operation handle.
            cancel_request(request_id)
            logger.info(
                "PeerSwap rendezvous response was no longer deliverable "
                "socket={} request_id={}",
                self.cln_socket,
                request_id,
            )
        finally:
            finish_request = getattr(self.handler, "finish_request", None)
            if callable(finish_request):
                finish_request(request_id)
            request_finished.set()

    @staticmethod
    def _send_error(
        rpc: LightningRpc,
        request_id: str,
        code: int,
        message: str,
    ) -> None:
        logger.error(
            "PeerSwap rendezvous response request_id={} error={} {}",
            request_id,
            code,
            message,
        )
        rpc.call(
            "jmpeerswap-response",
            {
                "request_id": request_id,
                "error": {"code": code, "message": message},
            },
        )


class PeerSwapRuntime:
    """Run a PeerSwap CLN RPC call with its rendezvous service active."""

    def __init__(
        self,
        operation: PeerSwapPrepareTxOperation,
        cln_socket: Path,
        pool_size: int = 4,
        rpc_factory: Callable[[str], LightningRpc] = LightningRpc,
    ) -> None:
        self.dispatcher = PeerSwapOperationDispatcher(operation)
        self.rendezvous = PeerSwapRendezvousClient(
            cln_socket=cln_socket,
            handler=self.dispatcher,
            pool_size=pool_size,
            rpc_factory=rpc_factory,
        )
        self.rpc = rpc_factory(str(cln_socket))

    def call(self, method: str, params: dict[str, object]) -> object:
        """Call PeerSwap through CLN while the rendezvous pool is running."""
        self.rendezvous.start()
        try:
            return self.rpc.call(method, params)
        finally:
            self.rendezvous.stop()


class PeerSwapOperationDispatcher:
    """Dispatch PeerSwap requests on a persistent asyncio event loop."""

    def __init__(self, operation: PeerSwapPrepareTxOperation) -> None:
        self.operation = operation
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._close_lock = threading.Lock()
        self._active: dict[str, tuple[Future[object], str]] = {}
        self._cancel_cleanup_requests: set[str] = set()
        self._pending_cleanup_count = 0
        self._cleanup_condition = threading.Condition(self._lock)
        self._closed = False

    def __call__(self, method: str, params: object) -> object:
        if method not in {"txprepare", "txsend", "txdiscard"}:
            raise ValueError(f"Unsupported PeerSwap request: {method}")
        future = self.start_request(f"direct-{id(params)}", method, params)
        return future.result()

    def start_request(
        self, request_id: str, method: str, params: object
    ) -> Future[object]:
        if method not in {"txprepare", "txsend", "txdiscard"}:
            raise ValueError(f"Unsupported PeerSwap request: {method}")
        with self._lock:
            loop = self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch(method, params),
                loop,
            )
            self._cancel_cleanup_requests.discard(request_id)
            self._active[request_id] = (future, method)
            return future

    def cancel_request(self, request_id: str) -> None:
        """Cancel an active request and clean up a completed txprepare.

        ``Future.cancel()`` is the transition attempt. If it returns False,
        completion may already have won the race, so a txprepare must be
        inspected rather than assuming that cancellation prevented preparation.
        """
        with self._lock:
            active = self._active.get(request_id)
        if active is None:
            return

        future, method = active
        cancelled = future.cancel()
        if cancelled:
            return

        if method != "txprepare":
            return

        with self._lock:
            if request_id in self._cancel_cleanup_requests:
                return
            self._cancel_cleanup_requests.add(request_id)
            self._pending_cleanup_count += 1

        def cleanup_completed(done: Future[object]) -> None:
            try:
                if done.cancelled():
                    return
                try:
                    result = done.result()
                except Exception:
                    return
                if not isinstance(result, dict):
                    return
                txid = result.get("txid")
                if not isinstance(txid, str):
                    return

                with self._lock:
                    loop = self._loop
                    loop_thread = self._loop_thread
                if loop is None:
                    loop = self._ensure_loop()
                    with self._lock:
                        loop_thread = self._loop_thread

                coroutine = self.operation.discard(txid.lower())
                if loop_thread is threading.current_thread():
                    loop.create_task(coroutine)
                    return

                discard = asyncio.run_coroutine_threadsafe(coroutine, loop)
                try:
                    discard.result()
                except Exception as exc:
                    logger.error(
                        "Failed to discard cancelled PeerSwap txprepare {}: {}",
                        txid,
                        exc,
                    )
            finally:
                with self._cleanup_condition:
                    self._pending_cleanup_count -= 1
                    if self._pending_cleanup_count == 0:
                        self._cleanup_condition.notify_all()

        with self._lock:
            cleanup_started = future.done()
            if not cleanup_started:
                future.add_done_callback(cleanup_completed)
        if cleanup_started:
            cleanup_completed(future)

    def finish_request(self, request_id: str) -> None:
        """Release the rendezvous operation handle after response delivery."""
        with self._lock:
            self._active.pop(request_id, None)

    def close(self) -> None:
        """Close prepared PeerSwap state and stop the event loop."""
        with self._close_lock:
            if self._closed:
                return
            with self._lock:
                loop = self._loop
                thread = self._loop_thread
                if loop is None:
                    loop = self._ensure_loop()
                    thread = self._loop_thread
                # Stop accepting new requests before waiting for the active
                # ones. In particular, txsend deliberately waits for an
                # in-flight broadcast after cancellation so shutdown must not
                # race operation.close() against that work.
                self._closed = True
                active = list(self._active.values())

            # Do not cancel active requests here. Their coroutines own cleanup
            # and txsend may have an externally visible broadcast in flight.
            # Waiting for the request to settle gives operation.close() a
            # stable view of _prepared state and avoids closing a wallet while
            # a request can still mutate it.
            for future, _ in active:
                try:
                    future.result()
                except Exception:
                    pass

            # A completed txprepare can still have a cancellation cleanup
            # callback running after the request itself has settled. Keep that
            # work accounted for so operation.close() cannot race its discard.
            with self._cleanup_condition:
                while self._pending_cleanup_count:
                    self._cleanup_condition.wait()

            close_future = asyncio.run_coroutine_threadsafe(
                self.operation.close(),
                loop,
            )
            try:
                close_future.result()
            finally:
                loop.call_soon_threadsafe(loop.stop)
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=5)
                self._loop = None
                self._loop_thread = None
                self._closed = True

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._closed:
            raise RuntimeError("PeerSwap operation dispatcher is closed")
        if self._loop is not None:
            return self._loop

        self._loop_ready.clear()
        thread = threading.Thread(
            target=self._run_loop,
            name="jm-lightning-peerswap-async",
            daemon=True,
        )
        self._loop_thread = thread
        thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError("PeerSwap operation event loop did not start")
        loop = self._loop
        if loop is None:
            raise RuntimeError("PeerSwap operation event loop failed to start")
        return loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _dispatch(self, method: str, params: object) -> object:
        if method == "txprepare":
            request = PeerSwapPrepareTxRequest.from_rpc(params)
            prepared = await self.operation.execute(request)
            return await self.operation.prepared_result(prepared.txid)

        if not isinstance(params, dict):
            raise ValueError(f"{method} params must be an object")
        txid = params.get("txid")
        if not isinstance(txid, str) or len(txid) != 64:
            raise ValueError(
                f"{method} requires a 64-character hexadecimal transaction id"
            )
        try:
            bytes.fromhex(txid)
        except ValueError as exc:
            raise ValueError(
                f"{method} requires a 64-character hexadecimal transaction id"
            ) from exc
        if method == "txsend":
            return await self.operation.send(txid.lower())
        return await self.operation.discard(txid.lower())
