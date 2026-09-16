from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from pyln.client.plugin import Request

import jmpeerswap.plugin as bridge_plugin
from jmlightning.operations.peerswap import PeerSwapRendezvousClient
from jmpeerswap.proxy import UnixRPCProxy
from jmpeerswap.rendezvous import PeerSwapRendezvous


class _WaitingClnRequest:
    """Minimal stand-in for CLN's outstanding jmpeerswap-request call."""

    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None
        self.exception: Exception | None = None
        self.event = threading.Event()

    def set_result(self, result: dict[str, Any]) -> None:
        self.result = result
        self.event.set()

    def set_exception(self, exc: Exception) -> None:
        self.exception = exc
        self.event.set()


def _start_upstream_socket(
    path: Path,
) -> tuple[socket.socket, threading.Event, threading.Thread]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    stopped = threading.Event()

    def accept() -> None:
        client, _ = listener.accept()
        try:
            stopped.wait()
        finally:
            client.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    return listener, stopped, thread


def _recv_json(stream: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = stream.recv(4096)
        if not chunk:
            raise RuntimeError("PeerSwap socket closed before receiving a response")
        data.extend(chunk)
    message = json.loads(data)
    assert isinstance(message, dict)
    return message


@pytest.mark.parametrize(
    ("method", "params", "result"),
    [
        (
            "txprepare",
            {
                "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
                "feerate": "urgent",
                "minconf": 1,
                "utxos": [],
            },
            {
                "unsigned_tx": "00" * 10,
                "txid": "11" * 32,
                "psbt": "cHNidP8BAHE=",
            },
        ),
        (
            "txsend",
            {"txid": "11" * 32},
            {
                "tx": "00" * 10,
                "txid": "11" * 32,
                "psbt": "cHNidP8BAHE=",
            },
        ),
        (
            "txdiscard",
            {"txid": "11" * 32},
            {
                "unsigned_tx": "00" * 10,
                "txid": "11" * 32,
            },
        ),
    ],
)
def test_real_cln_rendezvous_flow_preserves_peer_swap_request(
    tmp_path: Path,
    method: str,
    params: dict[str, Any],
    result: dict[str, str],
) -> None:
    """Exercise the complete proxy/rendezvous path around the CLN boundary.

    The CLN daemon itself is represented by the two RPC legs: jm-lightning's
    outstanding ``jmpeerswap-request`` and jm-peerswap's registered
    ``jmpeerswap-response``. The PeerSwap side is a real Unix RPC connection
    to the real proxy, so its original JSON-RPC request ID must survive the
    rendezvous unchanged.
    """
    cln_path = tmp_path / "lightning-rpc"
    peerswap_path = tmp_path / "peerswap-rpc"
    upstream_listener, upstream_stopped, upstream_thread = _start_upstream_socket(
        cln_path
    )
    rendezvous = PeerSwapRendezvous(request_timeout=1.0)
    proxy = UnixRPCProxy(
        peerswap_path,
        cln_path,
        lambda request, respond: bridge_plugin._handle_proxy_request(
            request, respond, rendezvous
        ),
    )

    waiter = _WaitingClnRequest()
    rendezvous.wait(cast(Request, waiter))
    proxy.start()

    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.connect(str(peerswap_path))
    try:
        peer.sendall(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 9001,
                        "method": method,
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )

        assert waiter.event.wait(1.0)
        assert waiter.exception is None
        assert waiter.result is not None
        request_id = waiter.result["request_id"]
        assert waiter.result["method"] == method
        assert waiter.result["params"] == params

        # This is jm-lightning's normal CLN RPC response to the outstanding
        # jmpeerswap-request. jm-peerswap then resolves the original PeerSwap
        # RPC request with this result.
        rendezvous.respond({"request_id": request_id, "result": result})

        response = _recv_json(peer)
        assert response == {
            "jsonrpc": "2.0",
            "id": 9001,
            "result": result,
        }
    finally:
        peer.close()
        rendezvous.stop()
        proxy.stop()
        upstream_stopped.set()
        upstream_listener.close()
        upstream_thread.join(timeout=1)


def test_real_proxy_reports_txsend_failure_and_allows_retry(tmp_path: Path) -> None:
    """A failed txsend response must not make the prepared state unrecoverable."""
    cln_path = tmp_path / "lightning-rpc"
    peerswap_path = tmp_path / "peerswap-rpc"
    upstream_listener, upstream_stopped, upstream_thread = _start_upstream_socket(
        cln_path
    )

    class StatefulHandler:
        def __init__(self) -> None:
            # Model a transaction that has already reached txprepare.
            self.prepared = True
            self.fail_once = True

        def __call__(self, method: str, params: Any) -> dict[str, str]:
            del params
            if method == "txprepare":
                self.prepared = True
                return {"unsigned_tx": "00" * 10, "txid": "11" * 32}
            if method == "txsend":
                assert self.prepared
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("broadcast failed")
                self.prepared = False
                return {"tx": "00" * 10, "txid": "11" * 32}
            if method == "txdiscard":
                self.prepared = False
                return {"unsigned_tx": "00" * 10, "txid": "11" * 32}
            raise AssertionError(method)

    handler = StatefulHandler()
    rendezvous = PeerSwapRendezvous(request_timeout=2.0)
    proxy = UnixRPCProxy(
        peerswap_path,
        cln_path,
        lambda request, respond: bridge_plugin._handle_proxy_request(
            request, respond, rendezvous
        ),
    )
    waiter = _WaitingClnRequest()
    rendezvous.wait(cast(Request, waiter))
    proxy.start()

    # This test exercises the proxy/rendezvous error transport. The stateful
    # handler models the JoinMarket operation: a broadcast failure leaves the
    # prepared transaction available for a later txsend.
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(peerswap_path))
    try:
        client.sendall(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "txsend",
                        "params": {"txid": "11" * 32},
                    }
                )
                + "\n"
            ).encode()
        )
        assert waiter.event.wait(1.0)
        assert waiter.exception is None
        assert waiter.result is not None

        # The CLN side reports the broadcast error correctly as an error, not
        # as a result containing an error object.
        with pytest.raises(RuntimeError, match="broadcast failed"):
            handler(waiter.result["method"], waiter.result["params"])
        rendezvous.respond(
            {
                "request_id": waiter.result["request_id"],
                "error": {"code": -32603, "message": "broadcast failed"},
            }
        )
        response = _recv_json(client)
        assert handler.prepared
        assert response == {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "broadcast failed"},
        }

        # A new rendezvous request for the same prepared tx can retry the
        # broadcast. The first failure must not have discarded the prepared
        # state.
        retry_waiter = _WaitingClnRequest()
        rendezvous.wait(cast(Request, retry_waiter))
        client.sendall(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "txsend",
                        "params": {"txid": "11" * 32},
                    }
                )
                + "\n"
            ).encode()
        )
        assert retry_waiter.event.wait(1.0)
        assert retry_waiter.exception is None
        assert retry_waiter.result is not None
        retry_result = handler(
            retry_waiter.result["method"], retry_waiter.result["params"]
        )
        rendezvous.respond(
            {
                "request_id": retry_waiter.result["request_id"],
                "result": retry_result,
            }
        )
        assert _recv_json(client) == {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"tx": "00" * 10, "txid": "11" * 32},
        }
        assert not handler.prepared
    finally:
        client.close()
        rendezvous.stop()
        proxy.stop()
        upstream_stopped.set()
        upstream_listener.close()
        upstream_thread.join(timeout=1)


def test_rendezvous_stop_fails_outstanding_proxy_request_without_leaking_state(
    tmp_path: Path,
) -> None:
    """Stopping the rendezvous service resolves outstanding PeerSwap RPCs."""
    cln_path = tmp_path / "lightning-rpc"
    peerswap_path = tmp_path / "peerswap-rpc"
    upstream_listener, upstream_stopped, upstream_thread = _start_upstream_socket(
        cln_path
    )
    rendezvous = PeerSwapRendezvous(request_timeout=30.0)
    proxy = UnixRPCProxy(
        peerswap_path,
        cln_path,
        lambda request, respond: bridge_plugin._handle_proxy_request(
            request, respond, rendezvous
        ),
    )
    waiter = _WaitingClnRequest()
    rendezvous.wait(cast(Request, waiter))
    proxy.start()

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(peerswap_path))
    try:
        client.sendall(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "txprepare",
                        "params": {
                            "outputs": [
                                {
                                    (
                                        "bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2"
                                    ): 100_000
                                }
                            ]
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        assert waiter.event.wait(1.0)
        assert waiter.result is not None

        rendezvous.stop()
        response = _recv_json(client)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert response["error"]["message"] == "PeerSwap rendezvous stopped"
        assert rendezvous._requests == {}
    finally:
        client.close()
        proxy.stop()
        upstream_stopped.set()
        upstream_listener.close()
        upstream_thread.join(timeout=1)


def test_four_worker_rendezvous_pool_handles_concurrent_requests() -> None:
    """The four-worker pool can have multiple CLN rendezvous calls in flight."""
    calls: list[tuple[str, Any]] = []
    entered = threading.Event()
    rpc_release = threading.Event()
    handler_release = threading.Event()
    active = 0
    max_active = 0
    entered_workers = 0
    state_lock = threading.Lock()
    request_counter = 0

    class Rpc:
        def call(self, method: str, params: Any = None) -> Any:
            nonlocal request_counter
            calls.append((method, params))
            if method != "jmpeerswap-request":
                return {"accepted": True}
            with state_lock:
                request_counter += 1
                request_id = request_counter
            return_queue = {
                "request_id": f"req-{request_id}",
                "method": "txsend",
                "params": {"txid": f"{request_id:064x}"},
            }
            nonlocal entered_workers
            with state_lock:
                entered_workers += 1
                if entered_workers >= 2:
                    entered.set()
            rpc_release.wait(timeout=2)
            return return_queue

    def handler(method: str, params: Any) -> dict[str, bool]:
        nonlocal active, max_active
        assert method == "txsend"
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            handler_release.wait(timeout=2)
            return {"ok": True}
        finally:
            with state_lock:
                active -= 1

    client = PeerSwapRendezvousClient(
        Path("/tmp/lightning-rpc"),
        handler,
        pool_size=4,
        rpc_factory=lambda _: cast(Any, Rpc()),
    )
    client.start()
    try:
        # Two workers must reach jmpeerswap-request before either is released.
        # If the pool were accidentally serialized, the event would not fire.
        assert entered.wait(timeout=2)
        rpc_release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with state_lock:
                if active >= 2:
                    break
            time.sleep(0.01)
        assert max_active >= 2
    finally:
        rpc_release.set()
        handler_release.set()
        client.stop()
