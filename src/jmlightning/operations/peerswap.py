from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from math import ceil
from pathlib import Path
from typing import Any

from jmcore.bitcoin import (
    ParsedTransaction,
    TxOutput,
    address_to_scriptpubkey,
    estimate_vsize,
    psbt_to_base64,
    serialize_transaction,
)
from loguru import logger

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
    def from_rpc(cls, params: Any) -> PeerSwapPrepareTxRequest:
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
                or not 0 < amount <= 0xFFFFFFFFFFFFFFFF
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
    phase: PeerSwapPhase = PeerSwapPhase.PREPARED

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

                change_address = jmadapter.get_change_address(self.config.mixdepth)
                tx, txid, psbt = tx_builder.build_and_sign_multifunding_tx(
                    plan=plan,
                    funding_addresses=[output.address for output in request.outputs],
                    change_address=change_address,
                    wallet=jmadapter.require_wallet(),
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
            )
            self._prepared[txid] = prepared
            logger.info(
                "PeerSwap JM operation txprepare complete socket={} txid={} inputs={}",
                self.cln_socket,
                txid,
                len(selected),
            )
            return prepared
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

        return self._prepare_result(prepared)

    async def send(self, txid: str) -> dict[str, str]:
        """Broadcast a prepared PeerSwap transaction and release its inputs."""
        prepared = self._prepared.get(txid)
        if prepared is None:
            raise ValueError(f"PeerSwap transaction {txid} is not prepared")
        prepared.require_phase(PeerSwapPhase.PREPARED)

        logger.info(
            "PeerSwap JM operation txsend start socket={} txid={}",
            self.cln_socket,
            txid,
        )
        broadcast_txid = await prepared.adapter.broadcast(prepared.tx)
        self._validate_txid(broadcast_txid, "JoinMarket broadcast transaction id")
        broadcast_txid = broadcast_txid.lower()
        if broadcast_txid != txid:
            raise RuntimeError(
                "JoinMarket broadcast returned an unexpected transaction id: "
                f"expected {txid}, got {broadcast_txid}"
            )

        # Broadcasting is the terminal transaction state. Remove the state
        # from the operation before cleanup so it cannot be sent twice even if
        # releasing JoinMarket resources subsequently reports an error.
        prepared.transition(PeerSwapPhase.PREPARED, PeerSwapPhase.BROADCAST)
        self._prepared.pop(txid)
        cleanup_errors: list[Exception] = []
        for coin in reversed(prepared.locked):
            try:
                prepared.adapter.unlock(coin)
            except Exception as exc:
                cleanup_errors.append(exc)
                logger.error(
                    "Failed to unlock {}:{} after PeerSwap broadcast: {}",
                    coin.utxo.txid,
                    coin.utxo.vout,
                    exc,
                )

        try:
            await prepared.adapter.close()
        except Exception as exc:
            cleanup_errors.append(exc)
            logger.error("Failed to close PeerSwap JoinMarket adapter: {}", exc)

        if cleanup_errors:
            logger.warning(
                "PeerSwap transaction {} was broadcast but cleanup encountered "
                "{} error(s)",
                txid,
                len(cleanup_errors),
            )

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

    async def close(self) -> None:
        """Release all prepared transactions and any pre-connected wallet."""
        for txid in list(self._prepared):
            try:
                await self.discard(txid)
            except Exception as exc:
                logger.error(
                    "Failed to discard prepared PeerSwap transaction {}: {}",
                    txid,
                    exc,
                )

        adapter = self._connected_adapter
        self._connected_adapter = None
        if adapter is not None:
            await adapter.close()

    async def discard(self, txid: str) -> dict[str, str]:
        """Release a prepared transaction and its JoinMarket UTXOs."""
        prepared = self._prepared.get(txid)
        if prepared is None:
            raise ValueError(f"PeerSwap transaction {txid} is not prepared")
        prepared.transition(PeerSwapPhase.PREPARED, PeerSwapPhase.DISCARDED)
        self._prepared.pop(txid)

        # Discard is terminal. The transition above happens before cleanup so
        # a partially failing cleanup cannot leave a transaction available for
        # a second lifecycle action.

        # First release every locked input so the transaction no longer owns
        # JoinMarket wallet state.
        cleanup_errors: list[Exception] = []
        for coin in reversed(prepared.locked):
            try:
                prepared.adapter.unlock(coin)
            except Exception as exc:
                cleanup_errors.append(exc)
                logger.error(
                    "Failed to unlock {}:{} while discarding PeerSwap transaction: {}",
                    coin.utxo.txid,
                    coin.utxo.vout,
                    exc,
                )

        # Always close the adapter, even when an input could not be unlocked.
        try:
            await prepared.adapter.close()
        except Exception as exc:
            cleanup_errors.append(exc)
            logger.error(
                "Failed to close PeerSwap JoinMarket adapter while discarding "
                "transaction {}: {}",
                txid,
                exc,
            )

        # The prepared state has already been removed, so report cleanup
        # failures without leaving a transaction that cannot be retried safely.
        if cleanup_errors:
            raise RuntimeError(
                f"Failed to fully clean up PeerSwap transaction {txid} "
                f"({len(cleanup_errors)} error(s))"
            ) from cleanup_errors[0]

        # CLN txdiscard returns the same unsigned transaction identity that
        # txprepare created, after releasing the reserved inputs.
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
        script = TxOutput.from_address(address, 1).script

        if len(script) == 22 and script[:2] == b"\x00\x14":
            return "p2wpkh"
        if len(script) == 34 and script[:2] == b"\x00\x20":
            return "p2wsh"
        if len(script) == 34 and script[:2] == b"\x51\x20":
            return "p2tr"
        if len(script) == 25 and script[:3] == b"\x76\xa9\x14":
            return "p2pkh"
        if len(script) == 23 and script[:2] == b"\xa9\x14":
            return "p2sh"

        raise ValueError("Unsupported PeerSwap funding output address type")
