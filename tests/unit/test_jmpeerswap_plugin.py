from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pyln.client import Plugin
from pyln.client.plugin import Request

import jmpeerswap.plugin as plugin
from jmpeerswap.proxy import UnixRPCProxy
from jmpeerswap.rendezvous import PeerSwapRendezvous


def _write_peer_swap_child(tmp_path: Path, body: str) -> str:
    executable = tmp_path / "peerswap-child"
    executable.write_text(
        "#!/usr/bin/env python3\n" + body,
    )
    executable.chmod(os.stat(executable).st_mode | 0o111)
    return str(executable)


def test_peer_swap_plugin_has_no_joinmarket_runtime_imports() -> None:
    tree = ast.parse(Path(plugin.__file__).read_text())

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    assert not any(
        module == "jmcore"
        or module.startswith("jmcore.")
        or module == "jmlightning"
        or module.startswith("jmlightning.")
        for module in imported_modules
    )
    assert "pyln.client" in imported_modules
    assert "jmpeerswap.proxy" in imported_modules


def test_peer_swap_executable_uses_configured_option() -> None:
    cln_plugin = Plugin()
    cln_plugin.options = {"peerswap-plugin": "/usr/local/bin/custom-peerswap"}

    assert plugin._peer_swap_executable(cln_plugin) == "/usr/local/bin/custom-peerswap"


def test_bridge_manifest_is_registered_before_init_without_starting_peerswap() -> None:
    cln_plugin = Plugin()
    process = plugin.PeerSwapProcess("/does/not/exist")

    plugin._register_rendezvous_methods(cln_plugin, plugin.PeerSwapRendezvous())
    plugin._register_peer_swap_manifest(
        cln_plugin, process, plugin.PEERSWAP_BRIDGE_MANIFEST
    )

    expected = {
        entry["name"] for entry in plugin.PEERSWAP_BRIDGE_MANIFEST["rpcmethods"]
    }
    assert expected <= set(cln_plugin.methods)
    assert process.process is None


def test_configured_executable_is_selected_without_replacing_registered_process(
    tmp_path: Path,
) -> None:
    executable = _write_peer_swap_child(tmp_path, "")
    process = plugin.PeerSwapProcess("peerswap")

    process.set_executable(plugin._resolve_peer_swap_executable(executable))

    assert process.executable == executable
    assert process.process is None


def test_missing_peer_swap_executable_is_rejected() -> None:
    with pytest.raises(FileNotFoundError, match="PeerSwap executable not found"):
        plugin._resolve_peer_swap_executable("/does/not/exist/peerswap")


def test_peer_swap_manifest_validation_allows_unsupported_liquid_methods() -> None:
    manifest = {
        **plugin.PEERSWAP_BRIDGE_MANIFEST,
        "rpcmethods": [
            *plugin.PEERSWAP_BRIDGE_MANIFEST["rpcmethods"],
            {"name": "peerswap-lbtc-getaddress"},
            {"name": "peerswap-lbtc-getbalance"},
            {"name": "peerswap-lbtc-sendtoaddress"},
        ],
    }

    plugin._validate_peer_swap_manifest(manifest)


def test_rendezvous_request_method_is_registered_as_background() -> None:
    from jmpeerswap.rendezvous import PeerSwapRendezvous

    cln_plugin = Plugin()
    rendezvous = PeerSwapRendezvous()

    plugin._register_rendezvous_methods(cln_plugin, rendezvous)

    assert cln_plugin.methods["jmpeerswap-request"].background is True
    assert cln_plugin.methods["jmpeerswap-response"].background is False


def test_rendezvous_response_method_accepts_rpc_kwargs() -> None:
    from jmpeerswap.rendezvous import PeerSwapRendezvous

    cln_plugin = Plugin()
    rendezvous = PeerSwapRendezvous()
    plugin._register_rendezvous_methods(cln_plugin, rendezvous)

    waiter = type(
        "Waiter",
        (),
        {
            "set_result": lambda self, result: setattr(self, "result", result),
            "set_exception": lambda self, exc: setattr(self, "exception", exc),
        },
    )()
    rendezvous.wait(cast(Request, waiter))

    responses: list[dict[str, Any]] = []
    rendezvous.submit(
        "txprepare",
        {"outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": "100000sat"}]},
        9,
        responses.append,
    )

    cln_plugin.methods["jmpeerswap-response"].func(
        request_id=waiter.result["request_id"],
        result={"txid": "11" * 32},
    )

    assert responses == [{"jsonrpc": "2.0", "id": 9, "result": {"txid": "11" * 32}}]


def test_rendezvous_response_method_accepts_rpc_error_kwargs() -> None:
    from jmpeerswap.rendezvous import PeerSwapRendezvous

    cln_plugin = Plugin()
    rendezvous = PeerSwapRendezvous()
    plugin._register_rendezvous_methods(cln_plugin, rendezvous)

    waiter = type(
        "Waiter",
        (),
        {
            "set_result": lambda self, result: setattr(self, "result", result),
            "set_exception": lambda self, exc: setattr(self, "exception", exc),
        },
    )()
    rendezvous.wait(cast(Request, waiter))

    responses: list[dict[str, Any]] = []
    rendezvous.submit(
        "txsend",
        {"txid": "11" * 32},
        9,
        responses.append,
    )

    cln_plugin.methods["jmpeerswap-response"].func(
        request_id=waiter.result["request_id"],
        error={"code": -32602, "message": "broadcast failed"},
    )

    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 9,
            "error": {"code": -32602, "message": "broadcast failed"},
        }
    ]


def test_proxy_request_handler_rendezvous_intercepts_transaction_calls() -> None:
    from jmpeerswap.rendezvous import PeerSwapRendezvous

    rendezvous = PeerSwapRendezvous()
    responses: list[dict[str, Any]] = []
    waiter = type(
        "Waiter",
        (),
        {
            "set_result": lambda self, result: setattr(self, "result", result),
            "set_exception": lambda self, exc: setattr(self, "exception", exc),
        },
    )()
    rendezvous.wait(cast(Request, waiter))

    request = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "txprepare",
        "params": {"outputs": []},
    }
    assert (
        plugin._handle_proxy_request(request, responses.append, rendezvous)
        is plugin.DEFERRED_RESPONSE
    )
    assert waiter.result["method"] == "txprepare"
    assert responses == []


def test_proxy_disconnect_uses_rendezvous_request_id() -> None:
    rendezvous = PeerSwapRendezvous()
    waiter = type(
        "Waiter",
        (),
        {
            "set_result": lambda self, result: setattr(self, "result", result),
            "set_exception": lambda self, exc: setattr(self, "exception", exc),
        },
    )()
    rendezvous.wait(cast(Request, waiter))

    disconnect_handler: list[Callable[[], None]] = []

    class ResponseSink:
        def __call__(self, response: dict[str, object]) -> None:
            pass

        def set_disconnect_handler(self, callback: Callable[[], None]) -> None:
            disconnect_handler.append(callback)

    request = {
        "jsonrpc": "2.0",
        "id": 17,
        "method": "txsend",
        "params": {"txid": "11" * 32},
    }

    assert (
        plugin._handle_proxy_request(request, ResponseSink(), rendezvous)
        is plugin.DEFERRED_RESPONSE
    )
    rendezvous_request_id = waiter.result["request_id"]
    assert rendezvous_request_id != "17"
    assert disconnect_handler

    disconnect_handler[0]()

    assert rendezvous_request_id not in rendezvous._requests
    assert rendezvous_request_id in rendezvous._terminal


def test_peer_swap_manifest_contract_is_fully_forwarded() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def request(self, method: str, params: Any) -> Any:
            self.calls.append((method, params))
            return {"method": method, "params": params}

    process = FakeProcess()
    cln_plugin = Plugin()
    plugin._register_peer_swap_manifest(
        cln_plugin,
        cast(plugin.PeerSwapProcess, process),
        plugin.PEERSWAP_BRIDGE_MANIFEST,
    )

    expected = {
        entry["name"] for entry in plugin.PEERSWAP_BRIDGE_MANIFEST["rpcmethods"]
    }
    assert expected <= set(cln_plugin.methods)

    class FakeRequest:
        def __init__(self, name: str) -> None:
            self.name = name
            self.result: Any = None
            self.exception: Exception | None = None
            self.event = threading.Event()

        def set_result(self, result: Any) -> None:
            self.result = result
            self.event.set()

        def set_exception(self, exc: Exception) -> None:
            self.exception = exc
            self.event.set()

    requests: list[FakeRequest] = []
    for name in sorted(expected):
        params = {"probe": name}
        request = FakeRequest(name)
        requests.append(request)
        assert cln_plugin.methods[name].background is True
        cln_plugin.methods[name].func(request=request, **params)

    for request in requests:
        assert request.event.wait(timeout=1)
        assert request.exception is None
        assert request.result == {
            "method": request.name,
            "params": {"probe": request.name},
        }

    assert sorted(method for method, _ in process.calls) == sorted(expected)


def test_peer_swap_manifest_methods_are_registered_without_renaming() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    cln_plugin = Plugin()
    plugin._register_rendezvous_methods(cln_plugin, plugin.PeerSwapRendezvous())

    plugin._register_peer_swap_manifest(
        cln_plugin,
        process,
        {
            "rpcmethods": [
                {
                    "name": "peerswap-swap-in",
                    "usage": "...",
                    "description": "swap in",
                },
                {
                    "name": "peerswap-swap-out",
                    "usage": "...",
                    "description": "swap out",
                },
            ],
            "options": [],
            "subscriptions": [],
            "hooks": [],
            "notifications": [],
        },
    )

    assert "peerswap-swap-in" in cln_plugin.methods
    assert "peerswap-swap-out" in cln_plugin.methods
    assert "jmpeerswap-swap-in" not in cln_plugin.methods


def test_peer_swap_manifest_does_not_reregister_shutdown_subscription() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    cln_plugin = Plugin()
    plugin._register_rendezvous_methods(cln_plugin, plugin.PeerSwapRendezvous())

    plugin._register_peer_swap_manifest(
        cln_plugin,
        process,
        {
            "rpcmethods": [],
            "options": [],
            "subscriptions": ["shutdown", "connect"],
            "hooks": [],
            "notifications": [],
        },
    )

    assert "shutdown" not in cln_plugin.subscriptions
    assert "connect" in cln_plugin.subscriptions


def test_peer_swap_manifest_string_custommsg_hook_returns_immediately() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, Any]] = []

    def request(method: str, params: Any) -> Any:
        calls.append((method, params))
        started.set()
        release.wait(timeout=1)
        return {"result": "ok"}

    process.request = request  # type: ignore[method-assign]
    cln_plugin = Plugin()

    plugin._register_peer_swap_manifest(
        cln_plugin,
        process,
        {
            "rpcmethods": [],
            "options": [],
            "subscriptions": [],
            "hooks": ["custommsg"],
            "notifications": [],
        },
    )

    result = cln_plugin.methods["custommsg"].func(
        type("Request", (), {"params": {"payload": "abc"}})()
    )
    assert result == {"result": "continue"}
    assert started.wait(timeout=1)
    assert calls == [("custommsg", {"payload": "abc"})]
    release.set()


def test_peer_swap_manifest_hook_forwards_non_custommsg_hooks_synchronously() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    calls: list[tuple[str, Any]] = []

    def request(method: str, params: Any) -> Any:
        calls.append((method, params))
        return {"result": "ok"}

    process.request = request  # type: ignore[method-assign]
    cln_plugin = Plugin()

    plugin._register_peer_swap_manifest(
        cln_plugin,
        process,
        {
            "rpcmethods": [],
            "options": [],
            "subscriptions": [],
            "hooks": [
                {"name": "htlc_accepted", "before": [], "after": [], "filters": []}
            ],
            "notifications": [],
        },
    )

    result = cln_plugin.methods["htlc_accepted"].func(
        type("Request", (), {"params": {"payload": "abc"}})()
    )
    assert result == {"result": "ok"}
    assert calls == [("htlc_accepted", {"payload": "abc"})]


def test_peer_swap_process_starts_and_requests_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []

    class FakeStream:
        def write(self, value: str) -> int:
            writes.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

        def readline(self) -> str:
            return (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"rpcmethods": []},
                    }
                )
                + "\n"
            )

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStream()
            self.stdout = FakeStream()

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float) -> None:
            return None

    fake_process = FakeProcess()

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        assert args == (["peerswap"],)
        assert kwargs["text"] is True
        assert kwargs["bufsize"] == 1
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    process = plugin.PeerSwapProcess("peerswap")
    process.start()

    assert writes[0].endswith("\n\n")
    assert json.loads(writes[0].strip()) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getmanifest",
        "params": {},
    }
    assert process.manifest == {"rpcmethods": []}

    process.stop()


def test_peer_swap_process_cleans_up_when_child_exits_during_manifest(
    tmp_path: Path,
) -> None:
    executable = _write_peer_swap_child(
        tmp_path,
        "import sys\nsys.stdin.readline()\nsys.exit(1)\n",
    )

    process = plugin.PeerSwapProcess(executable)

    with pytest.raises(RuntimeError, match="PeerSwap exited before responding"):
        process.start()

    assert process.process is None


def test_peer_swap_process_cleans_up_when_child_fails_during_initialise(
    tmp_path: Path,
) -> None:
    executable = _write_peer_swap_child(
        tmp_path,
        """
import json
import sys

for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    if request["method"] == "getmanifest":
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"rpcmethods": []},
                }
            ),
            flush=True,
        )
    elif request["method"] == "init":
        sys.exit(1)
""",
    )

    process = plugin.PeerSwapProcess(executable)
    process.start()
    process.initialise_async({}, {})

    with pytest.raises(RuntimeError, match="PeerSwap initialisation failed"):
        process.wait_initialised()

    assert process.process is None


def test_peer_swap_process_reports_child_exit_after_initialise(
    tmp_path: Path,
) -> None:
    executable = _write_peer_swap_child(
        tmp_path,
        """
import json
import sys

for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    result = {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"rpcmethods": []} if request["method"] == "getmanifest" else {},
    }
    print(json.dumps(result), flush=True)
    if request["method"] == "init":
        sys.exit(0)
""",
    )

    process = plugin.PeerSwapProcess(executable)
    process.start()
    process.initialise_async({}, {})
    process.wait_initialised()

    for _ in range(100):
        if process._process_error is not None:
            break
        threading.Event().wait(0.01)

    with pytest.raises(RuntimeError, match="PeerSwap child process failed"):
        process.request("peerswap-listswaps", {})

    process.stop()


def test_peer_swap_process_wait_initialised_stops_child_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugin, "PEERSWAP_INIT_TIMEOUT", 0.01)

    process = plugin.PeerSwapProcess("peerswap")

    with pytest.raises(TimeoutError, match="PeerSwap initialisation timed out"):
        process.wait_initialised()

    assert process.process is None


def test_peer_swap_process_initialise_async_sets_ready() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    initialised = threading.Event()

    def fake_initialise(options: dict[str, Any], configuration: dict[str, Any]) -> None:
        assert options == {"option": "value"}
        assert configuration == {"rpc-file": "/tmp/lightning-rpc"}
        initialised.set()

    process.initialise = fake_initialise  # type: ignore[method-assign]
    process.initialise_async(
        {"option": "value"},
        {"rpc-file": "/tmp/lightning-rpc"},
    )

    assert process._initialised.wait(timeout=1)
    assert initialised.is_set()
    process.wait_initialised()
    assert process._initialise_error is None


def test_peer_swap_process_initialise_async_preserves_error() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    error = RuntimeError("init failed")

    def fake_initialise(options: dict[str, Any], configuration: dict[str, Any]) -> None:
        del options, configuration
        raise error

    process.initialise = fake_initialise  # type: ignore[method-assign]
    process.initialise_async({}, {})

    assert process._initialised.wait(timeout=1)
    with pytest.raises(
        RuntimeError, match="PeerSwap initialisation failed"
    ) as exc_info:
        process.wait_initialised()
    assert exc_info.value.__cause__ is error


def test_peer_swap_process_forwards_child_rpc() -> None:
    process = plugin.PeerSwapProcess("peerswap")
    writes: list[dict[str, Any]] = []

    class FakeStream:
        def write(self, value: str) -> int:
            writes.append(json.loads(value))
            return len(value)

        def flush(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStream()

    process.process = FakeProcess()  # type: ignore[assignment]
    process.set_parent_rpc(lambda method, params: {"method": method, "params": params})

    process._handle_child_request(7, "txprepare", {"outputs": []})

    assert writes == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"method": "txprepare", "params": {"outputs": []}},
        }
    ]


def test_wire_parent_callbacks_forwards_rpc_and_notifications() -> None:
    cln_plugin = Plugin()

    class FakeRPC:
        def call(self, method: str, params: object) -> object:
            return {"method": method, "params": params}

    logs: list[tuple[str, str]] = []
    notifications: list[tuple[str, Any]] = []
    cln_plugin.rpc = FakeRPC()
    cln_plugin.log = lambda message, level="info": logs.append((message, level))
    cln_plugin.notify = lambda method, params: notifications.append((method, params))

    class FakeProcess:
        def __init__(self) -> None:
            self.rpc_callback: Callable[[str, object], object] = (  # fmt: skip
                lambda method, params: None
            )
            self.notification_callback: Callable[[str, object], None] = (
                lambda method, params: None
            )

        def set_parent_rpc(self, callback: Callable[[str, object], object]) -> None:
            self.rpc_callback = callback

        def set_parent_notification(
            self, callback: Callable[[str, object], None]
        ) -> None:
            self.notification_callback = callback

    process = FakeProcess()
    plugin._wire_parent_callbacks(cln_plugin, process)  # type: ignore[arg-type]

    assert process.rpc_callback("getinfo", {"foo": "bar"}) == {
        "method": "getinfo",
        "params": {"foo": "bar"},
    }
    process.notification_callback("log", {"level": "info", "message": "ready"})
    assert logs == [("ready", "info")]

    process.notification_callback("custom", {"value": 1})
    assert notifications == [("custom", {"value": 1})]


def test_rewrite_init() -> None:
    message = {
        "params": {
            "configuration": {
                "rpc-file": "/tmp/lightning/regtest/lightning-rpc",
                "lightning-dir": "/tmp/lightning/regtest",
                "network": "regtest",
            },
        },
    }

    cln_rpc, peerswap_rpc = plugin._rewrite_init(message)

    assert cln_rpc == Path("/tmp/lightning/regtest/lightning-rpc")
    assert peerswap_rpc == Path("/tmp/lightning/regtest/peerswap-rpc")
    assert message["params"]["configuration"]["rpc-file"] == "peerswap-rpc"
    assert message["params"]["configuration"]["network"] == "regtest"


def test_unix_rpc_proxy_forwards_rpc() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        upstream_path = directory_path / "lightning-rpc"
        listen_path = directory_path / "peerswap-rpc"

        ready = threading.Event()

        def upstream_server() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(upstream_path))
            server.listen(1)
            ready.set()

            client, _ = server.accept()
            try:
                request = client.recv(4096)
                assert request == (
                    b'{"jsonrpc":"2.0","id":7,"method":"getinfo","params":{}}\n'
                )
                client.sendall(b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n')
            finally:
                client.close()
                server.close()

        thread = threading.Thread(target=upstream_server)
        thread.start()
        ready.wait(timeout=2)

        proxy = UnixRPCProxy(listen_path, upstream_path)
        proxy.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(listen_path))
        client.sendall(b'{"jsonrpc":"2.0","id":7,"method":"getinfo","params":{}}\n')

        response = client.recv(4096)

        client.close()
        proxy.stop()
        thread.join(timeout=2)

        assert response == (b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n')
        assert not listen_path.exists()


def test_unix_rpc_proxy_closes_upstream_when_client_disconnects() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        upstream_path = directory_path / "lightning-rpc"
        listen_path = directory_path / "peerswap-rpc"

        ready = threading.Event()
        upstream_closed = threading.Event()

        def upstream_server() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(upstream_path))
            server.listen(1)
            ready.set()
            client, _ = server.accept()
            try:
                client.settimeout(2)
                assert client.recv(4096) == b"request\n"
                assert client.recv(4096) == b""
                upstream_closed.set()
            finally:
                client.close()
                server.close()

        thread = threading.Thread(target=upstream_server)
        thread.start()
        assert ready.wait(timeout=2)

        proxy = UnixRPCProxy(listen_path, upstream_path)
        proxy.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(listen_path))
        client.sendall(b"request\n")
        client.close()

        assert upstream_closed.wait(timeout=2)

        proxy.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not listen_path.exists()


def test_unix_rpc_proxy_closes_client_when_upstream_disconnects() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        upstream_path = directory_path / "lightning-rpc"
        listen_path = directory_path / "peerswap-rpc"

        ready = threading.Event()

        def upstream_server() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(upstream_path))
            server.listen(1)
            ready.set()
            client, _ = server.accept()
            client.close()
            server.close()

        thread = threading.Thread(target=upstream_server)
        thread.start()
        assert ready.wait(timeout=2)

        proxy = UnixRPCProxy(listen_path, upstream_path)
        proxy.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(listen_path))

        assert client.recv(4096) == b""

        client.close()
        proxy.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not listen_path.exists()


def test_unix_rpc_proxy_forwards_multiple_requests_on_one_connection() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        upstream_path = directory_path / "lightning-rpc"
        listen_path = directory_path / "peerswap-rpc"

        ready = threading.Event()

        def upstream_server() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(upstream_path))
            server.listen(1)
            ready.set()

            client, _ = server.accept()
            try:
                stream = client.makefile("rwb")
                for request, response in (
                    (
                        b'{"jsonrpc":"2.0","id":1,"method":"getinfo","params":{}}\n',
                        b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n',
                    ),
                    (
                        b'{"jsonrpc":"2.0","id":2,"method":"listfunds","params":{}}\n',
                        b'{"jsonrpc":"2.0","id":2,"result":{"outputs":[]}}\n',
                    ),
                ):
                    assert stream.readline() == request
                    stream.write(response)
                    stream.flush()
                stream.close()
            finally:
                client.close()
                server.close()

        thread = threading.Thread(target=upstream_server)
        thread.start()
        assert ready.wait(timeout=2)

        proxy = UnixRPCProxy(listen_path, upstream_path)
        proxy.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(listen_path))
        stream = client.makefile("rwb")

        requests = (
            b'{"jsonrpc":"2.0","id":1,"method":"getinfo","params":{}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"listfunds","params":{}}\n',
        )
        responses = (
            b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n',
            b'{"jsonrpc":"2.0","id":2,"result":{"outputs":[]}}\n',
        )

        for request, response in zip(requests, responses):
            stream.write(request)
            stream.flush()
            assert stream.readline() == response

        stream.close()
        client.close()
        proxy.stop()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert not listen_path.exists()


def test_unix_rpc_proxy_handles_handler_response_without_upstream() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        upstream_path = directory_path / "lightning-rpc"
        listen_path = directory_path / "peerswap-rpc"

        ready = threading.Event()
        handled: list[dict[str, Any]] = []

        def upstream_server() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(upstream_path))
            server.listen(1)
            ready.set()

            client, _ = server.accept()
            try:
                assert client.recv(4096) == b""
            finally:
                client.close()
                server.close()

        thread = threading.Thread(target=upstream_server)
        thread.start()
        assert ready.wait(timeout=2)

        def request_handler(
            request: dict[str, Any],
            respond: Any,
        ) -> dict[str, Any] | None:
            del respond
            handled.append(request)
            if request.get("method") != "txprepare":
                return None
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"tx": "funded"},
            }

        proxy = UnixRPCProxy(listen_path, upstream_path, request_handler)
        proxy.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(listen_path))
        client.sendall(b'{"jsonrpc":"2.0","id":9,"method":"txprepare","params":{}}\n')

        response = client.recv(4096)

        client.close()
        proxy.stop()
        thread.join(timeout=2)

        assert (
            response == b'{"jsonrpc": "2.0", "id": 9, "result": {"tx": "funded"}}\n\n'
        )
        assert handled == [
            {"jsonrpc": "2.0", "id": 9, "method": "txprepare", "params": {}}
        ]
        assert not thread.is_alive()


def test_unix_rpc_proxy_refuses_active_socket(tmp_path: Path) -> None:
    listen_path = tmp_path / "peerswap-rpc"
    upstream_path = tmp_path / "lightning-rpc"

    first = UnixRPCProxy(listen_path, upstream_path)
    first.start()
    try:
        second = UnixRPCProxy(listen_path, upstream_path)
        with pytest.raises(RuntimeError, match="already in use"):
            second.start()
        assert listen_path.exists()
    finally:
        first.stop()


def test_unix_rpc_proxy_does_not_remove_replaced_socket(tmp_path: Path) -> None:
    listen_path = tmp_path / "peerswap-rpc"
    upstream_path = tmp_path / "lightning-rpc"

    proxy = UnixRPCProxy(listen_path, upstream_path)
    proxy.start()

    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listen_path.unlink()
        replacement.bind(str(listen_path))
        replacement.listen(1)

        proxy.stop()

        assert listen_path.exists()
    finally:
        replacement.close()
        listen_path.unlink(missing_ok=True)


def test_unix_rpc_proxy_refuses_non_socket_path(
    tmp_path: Path,
) -> None:
    listen_path = tmp_path / "peerswap-rpc"
    upstream_path = tmp_path / "lightning-rpc"
    listen_path.write_text("not a socket")

    proxy = UnixRPCProxy(listen_path, upstream_path)

    with pytest.raises(RuntimeError, match="not a socket"):
        proxy.start()


def test_make_forwarder_accepts_manifest_rpc_parameters() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def request(self, method: str, params: Any) -> Any:
            self.calls.append((method, params))
            return {"ok": True}

    class FakeRequest:
        def __init__(self) -> None:
            self.result: Any = None
            self.exception: Exception | None = None
            self.event = threading.Event()

        def set_result(self, result: Any) -> None:
            self.result = result
            self.event.set()

        def set_exception(self, exc: Exception) -> None:
            self.exception = exc
            self.event.set()

    process = FakeProcess()
    request = FakeRequest()
    forward = plugin._make_forwarder(
        cast(plugin.PeerSwapProcess, process),
        "peerswap-addpeer",
        "Adds a peer to the allowlist file",
    )

    forward(request=request, peer_pubkey="02" + "11" * 32)

    assert request.event.wait(timeout=1)
    assert request.exception is None
    assert request.result == {"ok": True}
    assert process.calls == [("peerswap-addpeer", {"peer_pubkey": "02" + "11" * 32})]


def test_forward_method_preserves_peer_swap_method_name() -> None:
    class FakeRequest:
        params = {"short_channel_id": "1x2x3"}

    class FakeProcess:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def request(self, method: str, params: Any) -> Any:
            self.calls.append((method, params))
            return {"ok": True}

    process = FakeProcess()
    assert plugin._forward_method(
        FakeRequest(), cast(plugin.PeerSwapProcess, process), "peerswap-swap-in"
    ) == {"ok": True}
    assert process.calls == [("peerswap-swap-in", {"short_channel_id": "1x2x3"})]


def test_proxy_can_complete_intercepted_request_later() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        upstream_path = directory_path / "lightning-rpc"
        listen_path = directory_path / "peerswap-rpc"
        ready = threading.Event()

        upstream_data: list[bytes] = []

        def upstream_server() -> None:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(upstream_path))
            server.listen(1)
            ready.set()
            client, _ = server.accept()
            client.settimeout(0.5)
            try:
                try:
                    upstream_data.append(client.recv(4096))
                except TimeoutError:
                    upstream_data.append(b"")
            finally:
                client.close()
                server.close()

        upstream_thread = threading.Thread(target=upstream_server)
        upstream_thread.start()
        assert ready.wait(timeout=2)

        response_holder: list[Any] = []
        response_ready = threading.Event()

        def request_handler(
            request: dict[str, Any],
            respond: Any,
        ) -> dict[str, Any] | plugin.DeferredResponse | None:
            del request
            response_holder.append(respond)
            response_ready.set()
            return plugin.DEFERRED_RESPONSE

        proxy = UnixRPCProxy(listen_path, upstream_path, request_handler)
        proxy.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(listen_path))
        client.settimeout(2)
        client.sendall(b'{"jsonrpc":"2.0","id":9,"method":"txprepare","params":{}}\n')

        assert response_ready.wait(timeout=2)
        response_holder[0]({"jsonrpc": "2.0", "id": 9, "result": {"psbt": "abc"}})
        response = client.recv(4096)

        client.close()
        proxy.stop()
        upstream_thread.join(timeout=2)

        assert json.loads(response) == {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {"psbt": "abc"},
        }
        assert upstream_data == [b""]
