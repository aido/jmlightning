from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pyln.client import LightningRpc

from jmlightning.operations.peerswap import (
    PeerSwapOperationDispatcher,
    PeerSwapPrepareTxOperation,
    PeerSwapRendezvousClient,
    PeerSwapRuntime,
)


class FakeRpc:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def call(self, method: str, params: object = None) -> Any:
        self.calls.append((method, params))
        if method == "jmpeerswap-request":
            return {
                "request_id": "abc",
                "method": "txsend",
                "params": {"txid": "11" * 32},
            }
        return {"accepted": True}


def test_rendezvous_client_dispatches_and_responds() -> None:
    calls: list[tuple[str, object]] = []
    handled: list[tuple[str, object]] = []

    def rpc_factory(socket_path: str) -> FakeRpc:
        assert socket_path == "/tmp/lightning-rpc"
        return FakeRpc(calls)

    def handler(method: str, params: object) -> dict[str, bool]:
        handled.append((method, params))
        return {"ok": True}

    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        handler,
        pool_size=1,
        rpc_factory=cast(Callable[[str], LightningRpc], rpc_factory),
    )

    fake_rpc = rpc_factory("/tmp/lightning-rpc")
    client._handle_request(fake_rpc, fake_rpc.call("jmpeerswap-request"))

    assert handled == [("txsend", {"txid": "11" * 32})]
    assert calls[-1] == (
        "jmpeerswap-response",
        {"request_id": "abc", "result": {"ok": True}},
    )


def test_rendezvous_client_propagates_handler_error() -> None:
    calls: list[tuple[str, object]] = []

    class ErrorRpc(FakeRpc):
        pass

    rpc = ErrorRpc(calls)

    def handler(method: str, params: object) -> object:
        del method, params
        raise ValueError("invalid PeerSwap request")

    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        handler,
        pool_size=1,
        rpc_factory=lambda _: rpc,
    )

    client._handle_request(
        rpc,
        {
            "request_id": "req-1",
            "method": "txprepare",
            "params": {},
        },
    )

    assert calls[-1] == (
        "jmpeerswap-response",
        {
            "request_id": "req-1",
            "error": {"code": -32602, "message": "invalid PeerSwap request"},
        },
    )


def test_rendezvous_worker_drops_request_returned_after_stop() -> None:
    entered = threading.Event()
    release = threading.Event()
    handled: list[tuple[str, object]] = []

    class BlockingRpc:
        def call(self, method: str, params: object = None) -> Any:
            del params
            assert method == "jmpeerswap-request"
            entered.set()
            assert release.wait(timeout=2)
            return {
                "request_id": "late",
                "method": "txsend",
                "params": {},
            }

    def handler(method: str, params: object) -> None:
        handled.append((method, params))

    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        handler,
        pool_size=1,
        rpc_factory=lambda _: cast(LightningRpc, BlockingRpc()),
    )

    worker = threading.Thread(target=client._worker)
    worker.start()
    assert entered.wait(timeout=2)

    client._stopping.set()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert handled == []


def test_rendezvous_stop_waits_for_admitted_request_before_closing_handler() -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class BlockingRpc:
        def call(self, method: str, params: object = None) -> Any:
            del params
            assert method == "jmpeerswap-request"
            return {
                "request_id": "active",
                "method": "txsend",
                "params": {},
            }

    class BlockingHandler:
        def __call__(self, method: str, params: object) -> None:
            del method, params
            entered.set()
            assert release.wait(timeout=2)

        def close(self) -> None:
            closed.set()

    handler = BlockingHandler()
    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        cast(Callable[[str, object], object], handler),
        pool_size=1,
        rpc_factory=lambda _: cast(LightningRpc, BlockingRpc()),
    )

    worker = threading.Thread(target=client._worker)
    worker.start()
    assert entered.wait(timeout=2)

    stopper = threading.Thread(target=client.stop)
    stopper.start()
    assert not closed.wait(timeout=0.1)

    release.set()
    stopper.join(timeout=2)
    worker.join(timeout=2)

    assert not stopper.is_alive()
    assert not worker.is_alive()
    assert closed.is_set()


def test_dispatcher_close_closes_operation_before_stopping_loop() -> None:
    operation = Mock()
    operation.close = AsyncMock()

    dispatcher = PeerSwapOperationDispatcher(operation)
    dispatcher._ensure_loop()
    dispatcher.close()
    dispatcher.close()

    operation.close.assert_awaited_once()


def test_dispatcher_close_waits_for_pending_cancel_cleanup() -> None:
    from concurrent.futures import Future

    operation = Mock(spec=PeerSwapPrepareTxOperation)
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()

    async def discard(txid: str) -> dict[str, bool]:
        assert txid == "22" * 32
        cleanup_started.set()
        assert cleanup_release.wait(timeout=2)
        return {}

    operation.discard = discard
    operation.close = AsyncMock()
    dispatcher = PeerSwapOperationDispatcher(operation)
    future: Future[object] = Future()
    with dispatcher._lock:
        dispatcher._active["req-1"] = (future, "txprepare")

    future.set_result({"txid": "22" * 32})

    cancel_thread = threading.Thread(target=dispatcher.cancel_request, args=("req-1",))
    cancel_thread.start()
    assert cleanup_started.wait(timeout=1)

    close_thread = threading.Thread(target=dispatcher.close)
    close_thread.start()
    time.sleep(0.05)
    operation.close.assert_not_awaited()

    cleanup_release.set()
    cancel_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert not cancel_thread.is_alive()
    assert not close_thread.is_alive()
    operation.close.assert_awaited_once()


def test_dispatcher_close_waits_for_active_request_before_operation_close() -> None:
    from concurrent.futures import Future

    operation = Mock()
    operation.close = AsyncMock()
    dispatcher = PeerSwapOperationDispatcher(
        cast(PeerSwapPrepareTxOperation, operation)
    )

    started = threading.Event()
    release = threading.Event()

    class BlockingFuture(Future[object]):
        def result(self, timeout: float | None = None) -> object:
            started.set()
            if not release.wait(timeout):
                raise TimeoutError
            return None

    future = BlockingFuture()
    with dispatcher._lock:
        dispatcher._active["req-1"] = (future, "txsend")

    close_thread = threading.Thread(target=dispatcher.close)
    close_thread.start()

    assert started.wait(timeout=1)
    operation.close.assert_not_awaited()

    release.set()
    close_thread.join(timeout=2)

    assert not close_thread.is_alive()
    operation.close.assert_awaited_once()


def test_runtime_uses_dispatcher_as_rendezvous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeDispatcher:
        def __init__(self, operation: object) -> None:
            calls["dispatcher"] = self
            calls["operation"] = operation

    class FakeRendezvous:
        def __init__(self, **kwargs: object) -> None:
            calls["rendezvous"] = kwargs

        def start(self) -> None:
            calls["start"] = True

        def stop(self) -> None:
            calls["stop"] = True

    class FakeRpc:
        def call(self, method: str, params: object) -> dict[str, object]:
            calls["rpc"] = (method, params)
            return {"ok": True}

    monkeypatch.setattr(
        "jmlightning.operations.peerswap.PeerSwapOperationDispatcher",
        FakeDispatcher,
    )
    monkeypatch.setattr(
        "jmlightning.operations.peerswap.PeerSwapRendezvousClient",
        FakeRendezvous,
    )

    operation = object()
    runtime = PeerSwapRuntime(
        operation=cast(PeerSwapPrepareTxOperation, operation),
        cln_socket=Path("/tmp/lightning-rpc"),
        rpc_factory=lambda _: cast(LightningRpc, FakeRpc()),
    )

    rendezvous = cast(dict[str, object], calls["rendezvous"])
    assert rendezvous["handler"] is calls["dispatcher"]
    assert rendezvous["pool_size"] == 4
    assert runtime.call("peerswap-swap-in", {"amt_sat": 1000}) == {"ok": True}
    assert calls["start"] is True
    assert calls["stop"] is True
    assert calls["rpc"] == ("peerswap-swap-in", {"amt_sat": 1000})


def test_dispatcher_close_closes_operation_before_event_loop() -> None:
    operation = type("Operation", (), {"close": AsyncMock()})()
    dispatcher = PeerSwapOperationDispatcher(
        cast(PeerSwapPrepareTxOperation, operation)
    )

    dispatcher.close()
    dispatcher.close()

    operation.close.assert_awaited_once()


def test_rendezvous_client_rejects_invalid_pool_size() -> None:
    with pytest.raises(ValueError, match="pool_size must be positive"):
        PeerSwapRendezvousClient(Path("/tmp/lightning-rpc"), Mock(), pool_size=0)


def test_rendezvous_client_rejects_unsupported_method() -> None:
    calls: list[tuple[str, object]] = []
    rpc = FakeRpc(calls)
    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"), Mock(), rpc_factory=lambda _: rpc
    )

    client._handle_request(
        rpc,
        {"request_id": "req-1", "method": "unknown", "params": {}},
    )

    assert calls[-1] == (
        "jmpeerswap-response",
        {
            "request_id": "req-1",
            "error": {"code": -32601, "message": "Unsupported PeerSwap request"},
        },
    )


def test_rendezvous_client_converts_unexpected_handler_error_to_rpc_error() -> None:
    calls: list[tuple[str, object]] = []
    rpc = FakeRpc(calls)

    def handler(method: str, params: object) -> object:
        del method, params
        raise RuntimeError("backend failed")

    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"), handler, rpc_factory=lambda _: rpc
    )
    client._handle_request(
        rpc,
        {"request_id": "req-2", "method": "txsend", "params": {}},
    )

    assert calls[-1] == (
        "jmpeerswap-response",
        {
            "request_id": "req-2",
            "error": {"code": -32603, "message": "backend failed"},
        },
    )


def test_dispatcher_rejects_unsupported_method() -> None:
    operation = Mock()
    operation.close = AsyncMock()
    dispatcher = PeerSwapOperationDispatcher(
        cast(PeerSwapPrepareTxOperation, operation)
    )

    with pytest.raises(ValueError, match="Unsupported PeerSwap request"):
        dispatcher("unknown", {})

    dispatcher.close()


def test_dispatcher_rejects_invalid_transaction_id() -> None:
    operation = Mock()
    operation.close = AsyncMock()
    dispatcher = PeerSwapOperationDispatcher(
        cast(PeerSwapPrepareTxOperation, operation)
    )

    with pytest.raises(ValueError, match="64-character hexadecimal"):
        dispatcher("txsend", {"txid": "not-a-txid"})

    dispatcher.close()


def test_dispatcher_cleans_up_txprepare_when_cancel_loses_race() -> None:
    from concurrent.futures import Future

    operation = Mock(spec=PeerSwapPrepareTxOperation)
    operation.discard = AsyncMock(return_value={})
    dispatcher = PeerSwapOperationDispatcher(operation)
    future: Future[object] = Future()
    future.set_result({"txid": "22" * 32})
    with dispatcher._lock:
        dispatcher._active["req-1"] = (future, "txprepare")

    dispatcher.cancel_request("req-1")

    operation.discard.assert_awaited_once_with("22" * 32)
    dispatcher.close()


def test_dispatcher_cleans_up_txprepare_that_completes_after_cancel() -> None:
    from concurrent.futures import Future

    class NonCancellingFuture(Future[object]):
        def cancel(self) -> bool:
            return False

    operation = Mock(spec=PeerSwapPrepareTxOperation)
    operation.discard = AsyncMock(return_value={})
    dispatcher = PeerSwapOperationDispatcher(operation)
    future = NonCancellingFuture()
    with dispatcher._lock:
        dispatcher._active["req-1"] = (future, "txprepare")

    dispatcher.cancel_request("req-1")
    future.set_result({"txid": "22" * 32})

    operation.discard.assert_awaited_once_with("22" * 32)
    dispatcher.cancel_request("req-1")
    operation.discard.assert_awaited_once_with("22" * 32)
    dispatcher.close()


def test_dispatcher_inspects_completed_txprepare_when_cancel_fails() -> None:
    from concurrent.futures import Future

    class NonCancellingFuture(Future[object]):
        def cancel(self) -> bool:
            return False

    operation = Mock(spec=PeerSwapPrepareTxOperation)
    operation.discard = AsyncMock(return_value={})
    dispatcher = PeerSwapOperationDispatcher(operation)
    future = NonCancellingFuture()
    future.set_result({"txid": "22" * 32})
    with dispatcher._lock:
        dispatcher._active["req-1"] = (future, "txprepare")

    dispatcher.cancel_request("req-1")

    operation.discard.assert_awaited_once_with("22" * 32)
    dispatcher.close()


def test_rendezvous_cancel_watcher_cannot_cancel_after_dispatch_finishes() -> None:
    from concurrent.futures import Future

    cancel_entered = threading.Event()
    cancel_release = threading.Event()
    finished = threading.Event()
    cancelled = threading.Event()
    closed = threading.Event()

    class CancellableHandler:
        def start_request(
            self, request_id: str, method: str, params: object
        ) -> Future[object]:
            del request_id, method, params
            future: Future[object] = Future()
            future.set_result({"ok": True})
            return future

        def cancel_request(self, request_id: str) -> None:
            del request_id
            cancelled.set()

        def finish_request(self, request_id: str) -> None:
            del request_id
            finished.set()

        def close(self) -> None:
            closed.set()

    class CancelRpc:
        def call(self, method: str, params: object = None) -> Any:
            del params
            if method == "jmpeerswap-request":
                return {
                    "request_id": "req-1",
                    "method": "txsend",
                    "params": {},
                }
            if method == "jmpeerswap-cancel":
                cancel_entered.set()
                assert cancel_release.wait(timeout=2)
                return {"request_id": "req-1", "state": "cancelled"}
            return {"accepted": True}

    handler = CancellableHandler()
    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        cast(Callable[[str, object], object], handler),
        pool_size=1,
        rpc_factory=lambda _: cast(LightningRpc, CancelRpc()),
    )
    client.start()
    try:
        assert finished.wait(timeout=2)
        assert cancel_entered.wait(timeout=2)

        client.stop()
        assert closed.is_set()

        cancel_release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not cancelled.is_set():
            time.sleep(0.01)
        assert not cancelled.is_set()
    finally:
        cancel_release.set()
        client.stop()


def test_rendezvous_client_cancels_active_dispatch_on_peer_disconnect() -> None:
    from concurrent.futures import Future

    calls: list[tuple[str, object]] = []

    class CancellableHandler:
        def __init__(self) -> None:
            self.future: Future[object] = Future()
            self.cancelled = threading.Event()

        def start_request(
            self, request_id: str, method: str, params: object
        ) -> Future[object]:
            del request_id, method, params
            return self.future

        def cancel_request(self, request_id: str) -> None:
            del request_id
            self.cancelled.set()
            self.future.cancel()

    class CancelRpc(FakeRpc):
        def call(self, method: str, params: object = None) -> Any:
            calls.append((method, params))
            if method == "jmpeerswap-cancel":
                return {"request_id": "req-1", "state": "cancelled"}
            if method == "jmpeerswap-request":
                return {"request_id": "req-1", "method": "txsend", "params": {}}
            return {"accepted": True}

    handler = CancellableHandler()
    rpc = CancelRpc(calls)
    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        cast(Callable[[str, object], object], handler),
        pool_size=1,
        rpc_factory=lambda _: cast(LightningRpc, rpc),
    )

    client._handle_request(
        rpc,
        {"request_id": "req-1", "method": "txsend", "params": {}},
    )

    assert handler.cancelled.is_set()
    assert ("jmpeerswap-cancel", {"request_id": "req-1"}) in calls
    assert not any(method == "jmpeerswap-response" for method, _ in calls)
