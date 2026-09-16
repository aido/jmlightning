from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from pyln.client.plugin import Request

import jmpeerswap.plugin as bridge_plugin
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
