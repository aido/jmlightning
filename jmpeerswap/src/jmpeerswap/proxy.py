from __future__ import annotations

import json
import os
import socket
import stat
import threading
from collections.abc import Callable
from pathlib import Path


class DeferredResponse:
    """Marker for a request handled asynchronously by the proxy."""


DEFERRED_RESPONSE = DeferredResponse()


class _ProxyResponse:
    """Response sink which also owns deferred-request disconnect handling."""

    def __init__(self, client: socket.socket, write_lock: threading.Lock) -> None:
        self._client = client
        self._write_lock = write_lock
        self._disconnect_handler: Callable[[], None] | None = None
        self._disconnected = False
        self._lock = threading.Lock()

    def __call__(self, response: dict[str, object]) -> None:
        payload = json.dumps(response).encode() + b"\n\n"
        with self._write_lock:
            self._client.sendall(payload)

    def set_disconnect_handler(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._disconnected:
                call_now = True
            else:
                self._disconnect_handler = callback
                call_now = False
        if call_now:
            callback()

    def disconnect(self) -> None:
        with self._lock:
            if self._disconnected:
                return
            self._disconnected = True
            callback = self._disconnect_handler
            self._disconnect_handler = None
        if callback is not None:
            callback()


class UnixRPCProxy:
    """Proxy a Unix-domain RPC socket to another Unix-domain RPC socket."""

    def __init__(
        self,
        listen_path: Path,
        upstream_path: Path,
        request_handler: (
            Callable[
                [dict[str, object], Callable[[dict[str, object]], None]],
                dict[str, object] | DeferredResponse | None,
            ]
            | None
        ) = None,
    ) -> None:
        self.listen_path = listen_path
        self.upstream_path = upstream_path
        self.request_handler = request_handler
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("RPC proxy is already running")

        self._remove_stale_socket()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.listen_path))
            listener.listen()
            listener.settimeout(0.5)
        except Exception:
            listener.close()
            raise

        os.chmod(self.listen_path, stat.S_IRUSR | stat.S_IWUSR)

        self._listener = listener
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="jm-peerswap-rpc-proxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()

        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

        self._remove_socket()

    def _remove_stale_socket(self) -> None:
        try:
            mode = os.lstat(self.listen_path).st_mode
        except FileNotFoundError:
            return

        if not stat.S_ISSOCK(mode):
            raise RuntimeError(
                f"RPC proxy path exists and is not a socket: {self.listen_path}"
            )

        os.unlink(self.listen_path)

    def _remove_socket(self) -> None:
        try:
            mode = os.lstat(self.listen_path).st_mode
        except FileNotFoundError:
            return

        if stat.S_ISSOCK(mode):
            os.unlink(self.listen_path)

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return

        while not self._stopping.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                raise

            with self._clients_lock:
                self._clients.add(client)

            threading.Thread(
                target=self._proxy_client,
                args=(client,),
                name="jm-peerswap-rpc-client",
                daemon=True,
            ).start()

    def _proxy_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(str(self.upstream_path))

            relay_stopping = threading.Event()
            client_to_upstream = threading.Thread(
                target=self._relay_client,
                args=(client, upstream, relay_stopping),
                daemon=True,
            )
            upstream_to_client = threading.Thread(
                target=self._relay,
                args=(upstream, client, relay_stopping),
                daemon=True,
            )

            client_to_upstream.start()
            upstream_to_client.start()

            client_to_upstream.join()
            upstream_to_client.join()
        except OSError:
            if not self._stopping.is_set():
                return
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

            try:
                client.close()
            except OSError:
                pass

            with self._clients_lock:
                self._clients.discard(client)

    @staticmethod
    def _stop_relay(
        stopping: threading.Event,
        source: socket.socket,
        destination: socket.socket,
    ) -> None:
        if stopping.is_set():
            return
        stopping.set()
        for sock in (source, destination):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _relay_client(
        self,
        client: socket.socket,
        upstream: socket.socket,
        stopping: threading.Event,
    ) -> None:
        reader = client.makefile("rb")
        write_lock = threading.Lock()
        deferred: set[_ProxyResponse] = set()

        try:
            while True:
                data = reader.readline()
                if not data:
                    return

                response_sink = _ProxyResponse(client, write_lock)
                response = self._handle_request(data, response_sink)
                if response is DEFERRED_RESPONSE:
                    deferred.add(response_sink)
                    continue
                if response is None:
                    upstream.sendall(data)
                elif isinstance(response, dict):
                    response_sink(response)
                else:
                    raise TypeError(
                        "RPC request handler returned an unsupported response type"
                    )
        except OSError:
            return
        finally:
            reader.close()
            for response_sink in deferred:
                response_sink.disconnect()
            self._stop_relay(stopping, client, upstream)

    def _handle_request(
        self,
        data: bytes,
        respond: Callable[[dict[str, object]], None],
    ) -> dict[str, object] | DeferredResponse | None:
        if self.request_handler is None:
            return None

        try:
            request = json.loads(data)
        except json.JSONDecodeError:
            return None

        if not isinstance(request, dict):
            return None

        return self.request_handler(request, respond)

    @staticmethod
    def _relay(
        source: socket.socket,
        destination: socket.socket,
        stopping: threading.Event,
    ) -> None:
        try:
            while True:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination.sendall(data)
        except OSError:
            return
        finally:
            UnixRPCProxy._stop_relay(stopping, source, destination)
