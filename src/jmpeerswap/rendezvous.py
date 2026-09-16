from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pyln.client.plugin import Request


@dataclass(slots=True)
class _PeerSwapRequest:
    request_id: str
    peer_swap_id: Any
    method: str
    params: Any
    respond: Callable[[dict[str, Any]], None]
    timeout: threading.Timer | None = None


class PeerSwapRendezvous:
    """Match PeerSwap RPC requests with waiting JoinMarket RPC clients."""

    def __init__(self, max_pending: int = 32, request_timeout: float = 120.0) -> None:
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._max_pending = max_pending
        self._request_timeout = request_timeout
        self._waiters: deque[Request] = deque()
        self._requests: dict[str, _PeerSwapRequest] = {}
        self._unassigned: deque[str] = deque()
        self._lock = threading.Lock()
        self._stopped = False

    def wait(self, request: Request) -> None:
        """Hold a ``jmpeerswap-request`` call until work is available."""
        assignments: list[tuple[Request, dict[str, Any]]] = []
        error: Exception | None = None
        with self._lock:
            if self._stopped:
                error = RuntimeError("PeerSwap rendezvous is stopped")
            elif len(self._waiters) >= self._max_pending:
                error = RuntimeError("PeerSwap request pool is full")
            else:
                self._waiters.append(request)
                assignments = self._match_locked()

        if error is not None:
            request.set_exception(error)
            return
        self._complete_assignments(assignments)

    def submit(
        self,
        method: str,
        params: Any,
        peer_swap_id: Any,
        respond: Callable[[dict[str, Any]], None],
    ) -> None:
        """Queue an intercepted PeerSwap request for JoinMarket."""
        assignments: list[tuple[Request, dict[str, Any]]] = []
        error_response: dict[str, Any] | None = None
        with self._lock:
            if self._stopped:
                error_response = self._error_response(
                    peer_swap_id, -32603, "PeerSwap rendezvous is stopped"
                )
            elif len(self._requests) >= self._max_pending:
                error_response = self._error_response(
                    peer_swap_id, -32603, "PeerSwap request pool is full"
                )
            else:
                pending = _PeerSwapRequest(
                    request_id=uuid.uuid4().hex,
                    peer_swap_id=peer_swap_id,
                    method=method,
                    params=params,
                    respond=respond,
                )
                self._requests[pending.request_id] = pending
                self._unassigned.append(pending.request_id)
                pending.timeout = threading.Timer(
                    self._request_timeout,
                    self._expire_request,
                    args=(pending.request_id,),
                )
                pending.timeout.daemon = True
                pending.timeout.start()
                assignments = self._match_locked()

        if error_response is not None:
            self._safe_respond(respond, error_response)
            return
        self._complete_assignments(assignments)

    def respond(
        self,
        params: Any = None,
        *,
        request_id: str | None = None,
        result: Any = None,
        error: Any = None,
    ) -> None:
        if request_id is not None:
            params = {"request_id": request_id}
            if error is not None:
                params["error"] = error
            else:
                params["result"] = result

        if not isinstance(params, dict):
            raise ValueError("jmpeerswap-response params must be an object")

        request_id = params.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("jmpeerswap-response requires request_id")

        has_result = "result" in params
        has_error = "error" in params
        if has_result == has_error:
            raise ValueError(
                "jmpeerswap-response requires exactly one of result or error"
            )

        with self._lock:
            pending = self._requests.pop(request_id, None)
            if pending is not None and pending.timeout is not None:
                pending.timeout.cancel()

        if pending is None:
            raise ValueError(f"Unknown PeerSwap rendezvous request: {request_id}")

        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": pending.peer_swap_id,
        }
        if has_error:
            error = params["error"]
            if not isinstance(error, dict):
                raise ValueError("jmpeerswap-response error must be an object")
            response["error"] = error
        else:
            response["result"] = params["result"]

        logger.info(
            "PeerSwap rendezvous respond peer_swap_id={} request_id={} error={}",
            pending.peer_swap_id,
            request_id,
            has_error,
        )
        self._safe_respond(pending.respond, response)

    def stop(self) -> None:
        """Fail all outstanding rendezvous calls and stop accepting new ones."""
        with self._lock:
            self._stopped = True
            waiters = list(self._waiters)
            self._waiters.clear()
            requests = list(self._requests.values())
            self._requests.clear()
            self._unassigned.clear()
            for pending in requests:
                if pending.timeout is not None:
                    pending.timeout.cancel()

        for request in waiters:
            request.set_exception(RuntimeError("PeerSwap rendezvous stopped"))
        for pending in requests:
            self._safe_respond(
                pending.respond,
                self._error_response(
                    pending.peer_swap_id,
                    -32603,
                    "PeerSwap rendezvous stopped",
                ),
            )

    def _match_locked(self) -> list[tuple[Request, dict[str, Any]]]:
        assignments: list[tuple[Request, dict[str, Any]]] = []
        while self._waiters and self._unassigned:
            waiter = self._waiters.popleft()
            request_id = self._unassigned.popleft()
            pending = self._requests.get(request_id)
            if pending is None:
                continue
            assignments.append(
                (
                    waiter,
                    {
                        "request_id": pending.request_id,
                        "method": pending.method,
                        "params": pending.params,
                    },
                )
            )
        return assignments

    def _complete_assignments(
        self,
        assignments: list[tuple[Request, dict[str, Any]]],
    ) -> None:
        for waiter, result in assignments:
            logger.info(
                "PeerSwap rendezvous match method={} request_id={}",
                result.get("method"),
                result.get("request_id"),
            )
            waiter.set_result(result)

    def _expire_request(self, request_id: str) -> None:
        with self._lock:
            pending = self._requests.pop(request_id, None)
            if pending is None or self._stopped:
                return
            try:
                self._unassigned.remove(request_id)
            except ValueError:
                pass

        logger.error(
            "PeerSwap rendezvous expired peer_swap_id={} request_id={}",
            pending.peer_swap_id,
            request_id,
        )
        self._safe_respond(
            pending.respond,
            self._error_response(
                pending.peer_swap_id,
                -32001,
                "PeerSwap rendezvous request timed out",
            ),
        )

    @staticmethod
    def _safe_respond(
        respond: Callable[[dict[str, Any]], None],
        response: dict[str, Any],
    ) -> None:
        try:
            respond(response)
        except OSError:
            # The PeerSwap RPC connection may have disappeared while the
            # JoinMarket operation was outstanding. The request has already
            # been removed from the rendezvous state, so there is nothing
            # further to clean up.
            return

    @staticmethod
    def _error_response(
        request_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
