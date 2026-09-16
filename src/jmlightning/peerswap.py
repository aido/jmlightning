from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger
from pyln.client import LightningRpc

from jmlightning.operations.peerswap import (
    PeerSwapPrepareTxOperation,
    PeerSwapPrepareTxRequest,
)


class PeerSwapRendezvousClient:
    """Maintain CLN RPC waiters for requests arriving from jm-peerswap."""

    def __init__(
        self,
        cln_socket: Path,
        handler: Callable[[str, Any], Any],
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
                logger.info(
                    "PeerSwap rendezvous client socket={} "
                    "received method={} request_id={}",
                    self.cln_socket,
                    request.get("method") if isinstance(request, dict) else "?",
                    request.get("request_id") if isinstance(request, dict) else "?",
                )
                self._handle_request(rpc, request)
            except Exception as exc:
                logger.exception(
                    "PeerSwap rendezvous client socket={} failed: {}",
                    self.cln_socket,
                    exc,
                )
                if self._stopping.is_set():
                    return
                continue

    def _handle_request(self, rpc: LightningRpc, request: Any) -> None:
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

    def call(self, method: str, params: dict[str, Any]) -> Any:
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
        self._closed = False

    def __call__(self, method: str, params: Any) -> Any:
        if method not in {"txprepare", "txsend", "txdiscard"}:
            raise ValueError(f"Unsupported PeerSwap request: {method}")
        with self._lock:
            loop = self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch(method, params),
                loop,
            )
            return future.result()

    def close(self) -> None:
        """Close prepared PeerSwap state and stop the event loop."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._loop_thread
            if loop is not None:
                future = asyncio.run_coroutine_threadsafe(
                    self.operation.close(),
                    loop,
                )
                try:
                    future.result(timeout=5)
                except Exception as exc:
                    logger.error("Failed to close PeerSwap operation: {}", exc)
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)
            self._loop = None
            self._loop_thread = None

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

    async def _dispatch(self, method: str, params: Any) -> Any:
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
