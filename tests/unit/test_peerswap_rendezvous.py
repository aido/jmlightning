from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pyln.client import LightningRpc

from jmlightning.operations.peerswap import (
    PeerSwapOperationDispatcher,
    PeerSwapRendezvousClient,
    PeerSwapRuntime,
)


class FakeRpc:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self.calls = calls

    def call(self, method: str, params: Any = None) -> Any:
        self.calls.append((method, params))
        if method == "jmpeerswap-request":
            return {
                "request_id": "abc",
                "method": "txsend",
                "params": {"txid": "11" * 32},
            }
        return {"accepted": True}


def test_rendezvous_client_dispatches_and_responds() -> None:
    calls: list[tuple[str, Any]] = []
    handled: list[tuple[str, Any]] = []

    def rpc_factory(socket_path: str) -> FakeRpc:
        assert socket_path == "/tmp/lightning-rpc"
        return FakeRpc(calls)

    def handler(method: str, params: Any) -> dict[str, bool]:
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
    calls: list[tuple[str, Any]] = []

    class ErrorRpc(FakeRpc):
        pass

    rpc = ErrorRpc(calls)

    def handler(method: str, params: Any) -> Any:
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


def test_dispatcher_close_closes_operation_before_stopping_loop() -> None:
    operation = Mock()
    operation.close = AsyncMock()

    dispatcher = PeerSwapOperationDispatcher(operation)
    dispatcher._ensure_loop()
    dispatcher.close()
    dispatcher.close()

    operation.close.assert_awaited_once()


def test_runtime_uses_dispatcher_as_rendezvous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeDispatcher:
        def __init__(self, operation: Any) -> None:
            calls["dispatcher"] = self
            calls["operation"] = operation

    class FakeRendezvous:
        def __init__(self, **kwargs: Any) -> None:
            calls["rendezvous"] = kwargs

        def start(self) -> None:
            calls["start"] = True

        def stop(self) -> None:
            calls["stop"] = True

    class FakeRpc:
        def call(self, method: str, params: Any) -> dict[str, Any]:
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
        operation=cast(Any, operation),
        cln_socket=Path("/tmp/lightning-rpc"),
        rpc_factory=lambda _: cast(Any, FakeRpc()),
    )

    assert calls["rendezvous"]["handler"] is calls["dispatcher"]
    assert calls["rendezvous"]["pool_size"] == 4
    assert runtime.call("peerswap-swap-in", {"amt_sat": 1000}) == {"ok": True}
    assert calls["start"] is True
    assert calls["stop"] is True
    assert calls["rpc"] == ("peerswap-swap-in", {"amt_sat": 1000})


def test_dispatcher_close_closes_operation_before_event_loop() -> None:
    operation = type("Operation", (), {"close": AsyncMock()})()
    dispatcher = PeerSwapOperationDispatcher(cast(Any, operation))

    dispatcher.close()
    dispatcher.close()

    operation.close.assert_awaited_once()
