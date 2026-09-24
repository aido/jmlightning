from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto

from loguru import logger
from pyln.client.plugin import Request


class PeerSwapRendezvousState(StrEnum):
    """Lifecycle state of an intercepted PeerSwap transaction request."""

    QUEUED = auto()
    ASSIGNED = auto()
    COMPLETED = auto()
    CANCELLED = auto()
    TIMED_OUT = auto()


@dataclass(slots=True)
class _PeerSwapRequest:
    request_id: str
    peer_swap_id: object
    method: str
    params: object
    respond: Callable[[dict[str, object]], None]
    state: PeerSwapRendezvousState = PeerSwapRendezvousState.QUEUED
    timeout: threading.Timer | None = None
    cancel_waiter: Request | None = None
    terminal_expires_at: float | None = None


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
        self._terminal: dict[str, _PeerSwapRequest] = {}
        self._unassigned: deque[str] = deque()
        self._lock = threading.Lock()
        self._stopped = False

    def _purge_terminal_locked(self) -> None:
        now = time.monotonic()
        for request_id, pending in list(self._terminal.items()):
            if (
                pending.terminal_expires_at is not None
                and pending.terminal_expires_at <= now
            ):
                self._terminal.pop(request_id, None)

    def wait(self, request: Request) -> None:
        """Hold a ``jmpeerswap-request`` call until work is available."""
        assignments: list[tuple[Request, dict[str, object]]] = []
        error: Exception | None = None
        with self._lock:
            self._purge_terminal_locked()
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
        params: object,
        peer_swap_id: object,
        respond: Callable[[dict[str, object]], None],
    ) -> str | None:
        """Queue an intercepted PeerSwap request for JoinMarket.

        Return the bridge-generated rendezvous request ID when the request is
        accepted. The ID is deliberately distinct from PeerSwap's JSON-RPC ID.
        """
        assignments: list[tuple[Request, dict[str, object]]] = []
        error_response: dict[str, object] | None = None
        request_id: str | None = None
        with self._lock:
            self._purge_terminal_locked()
            if self._stopped:
                error_response = self._error_response(
                    peer_swap_id, -32603, "PeerSwap rendezvous is stopped"
                )
            elif len(self._requests) >= self._max_pending:
                error_response = self._error_response(
                    peer_swap_id, -32603, "PeerSwap request pool is full"
                )
            else:
                request_id = uuid.uuid4().hex
                pending = _PeerSwapRequest(
                    request_id=request_id,
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
            return None
        self._complete_assignments(assignments)
        return request_id

    def wait_cancel(self, request: Request) -> None:
        """Wait for the assigned operation to be cancelled or completed.

        This is deliberately a separate long-poll from ``jmpeerswap-request``.
        Once a request is assigned, cancellation ownership belongs to the
        JoinMarket operation rather than to the rendezvous timeout. The
        operation therefore watches this endpoint until the PeerSwap side
        disconnects or the operation completes normally.
        """
        params = request.params
        if not isinstance(params, dict):
            request.set_exception(
                ValueError("jmpeerswap-cancel params must be an object")
            )
            return
        request_id = params.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            request.set_exception(ValueError("jmpeerswap-cancel requires request_id"))
            return

        immediate: dict[str, object] | None = None
        exception: Exception | None = None
        with self._lock:
            self._purge_terminal_locked()
            pending = self._requests.get(request_id)
            if pending is not None:
                if pending.state is PeerSwapRendezvousState.ASSIGNED:
                    if pending.cancel_waiter is not None:
                        exception = RuntimeError(
                            "PeerSwap cancellation watcher already exists for "
                            f"{request_id}"
                        )
                    else:
                        pending.cancel_waiter = request
                        return
                else:
                    exception = RuntimeError(
                        f"PeerSwap rendezvous request {request_id} is not assigned"
                    )
            else:
                pending = self._terminal.pop(request_id, None)
                if pending is None:
                    exception = ValueError(
                        f"Unknown PeerSwap rendezvous request: {request_id}"
                    )
                else:
                    immediate = {
                        "request_id": request_id,
                        "state": pending.state.value,
                    }

        if exception is not None:
            request.set_exception(exception)
        elif immediate is not None:
            try:
                request.set_result(immediate)
            except OSError:
                return

    @staticmethod
    def _set_cancel_result(
        request: Request, request_id: str, state: PeerSwapRendezvousState
    ) -> None:
        try:
            request.set_result({"request_id": request_id, "state": state.value})
        except OSError:
            return

    def respond(
        self,
        params: object = None,
        *,
        request_id: str | None = None,
        result: object = None,
        error: str | None = None,
    ) -> None:
        if request_id is not None:
            response_params: dict[str, object] = {"request_id": request_id}
            if error is not None:
                response_params["error"] = error
            else:
                response_params["result"] = result
            params = response_params

        if not isinstance(params, dict):
            raise ValueError("jmpeerswap-response params must be an object")

        request_id_value = params.get("request_id")
        if not isinstance(request_id_value, str) or not request_id_value:
            raise ValueError("jmpeerswap-response requires request_id")
        request_id = request_id_value

        has_result = "result" in params
        has_error = "error" in params
        if has_result == has_error:
            raise ValueError(
                "jmpeerswap-response requires exactly one of result or error"
            )
        if has_error and not isinstance(params["error"], dict):
            raise ValueError("jmpeerswap-response error must be an object")

        cancel_waiter: Request | None = None
        with self._lock:
            self._purge_terminal_locked()
            pending = self._requests.get(request_id)
            if pending is None:
                raise ValueError(f"Unknown PeerSwap rendezvous request: {request_id}")
            if pending.state is not PeerSwapRendezvousState.ASSIGNED:
                raise ValueError(
                    f"PeerSwap rendezvous request {request_id} is already "
                    f"{pending.state.value}"
                )
            pending.state = PeerSwapRendezvousState.COMPLETED
            self._requests.pop(request_id)
            self._terminal[request_id] = pending
            if pending.timeout is not None:
                pending.timeout.cancel()
            cancel_waiter = pending.cancel_waiter
            pending.cancel_waiter = None
            pending.terminal_expires_at = time.monotonic() + self._request_timeout

        response: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": pending.peer_swap_id,
        }
        if has_error:
            response["error"] = params["error"]
        else:
            response["result"] = params["result"]

        logger.info(
            "PeerSwap rendezvous respond peer_swap_id={} request_id={} error={}",
            pending.peer_swap_id,
            request_id,
            has_error,
        )
        delivered = self._safe_respond(pending.respond, response)
        if not delivered:
            pending.state = PeerSwapRendezvousState.CANCELLED
        if cancel_waiter is not None:
            self._set_cancel_result(
                cancel_waiter,
                request_id,
                (
                    PeerSwapRendezvousState.COMPLETED
                    if delivered
                    else PeerSwapRendezvousState.CANCELLED
                ),
            )
            with self._lock:
                self._terminal.pop(request_id, None)

    def cancel(self, request_id: str) -> None:
        """Cancel a request because the original PeerSwap connection closed."""
        cancel_waiter: Request | None = None
        pending: _PeerSwapRequest | None = None
        with self._lock:
            self._purge_terminal_locked()
            pending = self._requests.get(request_id)
            if pending is None:
                return
            if pending.state not in {
                PeerSwapRendezvousState.QUEUED,
                PeerSwapRendezvousState.ASSIGNED,
            }:
                return

            was_assigned = pending.state is PeerSwapRendezvousState.ASSIGNED
            pending.state = PeerSwapRendezvousState.CANCELLED
            self._requests.pop(request_id)
            try:
                self._unassigned.remove(request_id)
            except ValueError:
                pass
            if pending.timeout is not None:
                pending.timeout.cancel()
            cancel_waiter = pending.cancel_waiter
            pending.cancel_waiter = None
            if was_assigned:
                pending.terminal_expires_at = time.monotonic() + self._request_timeout
                self._terminal[request_id] = pending

        logger.info(
            "PeerSwap rendezvous cancelled peer_swap_id={} request_id={}",
            pending.peer_swap_id,
            request_id,
        )
        if cancel_waiter is not None:
            cancel_waiter.set_result(
                {
                    "request_id": request_id,
                    "state": PeerSwapRendezvousState.CANCELLED.value,
                }
            )
            with self._lock:
                self._terminal.pop(request_id, None)

    def stop(self) -> None:
        """Fail all outstanding rendezvous calls and stop accepting new ones."""
        with self._lock:
            self._purge_terminal_locked()
            self._stopped = True
            waiters = list(self._waiters)
            self._waiters.clear()
            requests = list(self._requests.values())
            self._requests.clear()
            self._unassigned.clear()
            for pending in requests:
                if pending.timeout is not None:
                    pending.timeout.cancel()
                pending.state = PeerSwapRendezvousState.CANCELLED
            cancel_waiters = [
                (pending.request_id, pending.cancel_waiter)
                for pending in requests
                if pending.cancel_waiter is not None
            ]
            for pending in requests:
                pending.cancel_waiter = None

        for request in waiters:
            request.set_exception(RuntimeError("PeerSwap rendezvous stopped"))
        for request_id, request in cancel_waiters:
            self._set_cancel_result(
                request, request_id, PeerSwapRendezvousState.CANCELLED
            )
        for pending in requests:
            self._safe_respond(
                pending.respond,
                self._error_response(
                    pending.peer_swap_id,
                    -32603,
                    "PeerSwap rendezvous stopped",
                ),
            )

    def _match_locked(self) -> list[tuple[Request, dict[str, object]]]:
        assignments: list[tuple[Request, dict[str, object]]] = []
        while self._waiters and self._unassigned:
            waiter = self._waiters.popleft()
            request_id = self._unassigned.popleft()
            pending = self._requests.get(request_id)
            if pending is None or pending.state is not PeerSwapRendezvousState.QUEUED:
                continue
            pending.state = PeerSwapRendezvousState.ASSIGNED
            if pending.timeout is not None:
                pending.timeout.cancel()
                pending.timeout = None
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
        assignments: list[tuple[Request, dict[str, object]]],
    ) -> None:
        for waiter, result in assignments:
            logger.info(
                "PeerSwap rendezvous match method={} request_id={}",
                result.get("method"),
                result.get("request_id"),
            )
            waiter.set_result(result)

    def _expire_request(self, request_id: str) -> None:
        cancel_waiter: Request | None = None
        with self._lock:
            pending = self._requests.get(request_id)
            if pending is None or self._stopped:
                return
            if pending.state is PeerSwapRendezvousState.QUEUED:
                pending.state = PeerSwapRendezvousState.TIMED_OUT
                self._requests.pop(request_id)
                try:
                    self._unassigned.remove(request_id)
                except ValueError:
                    pass
            else:
                # Once assigned, the JoinMarket operation owns cancellation and
                # lifetime. The rendezvous timeout only protects the queue
                # before an operation has taken ownership.
                return

        logger.error(
            "PeerSwap rendezvous expired peer_swap_id={} request_id={} state={}",
            pending.peer_swap_id,
            request_id,
            pending.state.value,
        )
        if cancel_waiter is not None:
            self._set_cancel_result(
                cancel_waiter, request_id, PeerSwapRendezvousState.TIMED_OUT
            )
            with self._lock:
                self._terminal.pop(request_id, None)
            return

        self._safe_respond(
            pending.respond,
            self._error_response(
                pending.peer_swap_id,
                -32001,
                "PeerSwap rendezvous request timed out",
            ),
        )
        with self._lock:
            self._terminal.pop(request_id, None)

    @staticmethod
    def _safe_respond(
        respond: Callable[[dict[str, object]], None],
        response: dict[str, object],
    ) -> bool:
        try:
            respond(response)
        except OSError:
            # The PeerSwap RPC connection may have disappeared while the
            # JoinMarket operation was outstanding. Let the cancellation
            # watcher reclaim a completed txprepare instead of leaking its
            # prepared JoinMarket transaction.
            return False
        return True

    @staticmethod
    def _error_response(
        request_id: object,
        code: int,
        message: str,
    ) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
