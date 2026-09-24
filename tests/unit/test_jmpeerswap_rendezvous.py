from __future__ import annotations

import time
from typing import Any, cast

import pytest
from pyln.client.plugin import Request

from jmpeerswap.rendezvous import PeerSwapRendezvous


class FakeRequest:
    def __init__(self) -> None:
        self.results: list[Any] = []
        self.exceptions: list[Exception] = []
        self.params: object = {}

    def set_result(self, result: Any) -> None:
        self.results.append(result)

    def set_exception(self, exc: Exception) -> None:
        self.exceptions.append(exc)


def test_rendezvous_matches_waiter_to_peer_swap_request() -> None:
    rendezvous = PeerSwapRendezvous()
    waiter = FakeRequest()
    responses: list[dict[str, Any]] = []

    rendezvous.wait(cast(Request, waiter))
    rendezvous.submit("txprepare", {"outputs": []}, 17, responses.append)

    assert waiter.results[0]["method"] == "txprepare"
    request_id = waiter.results[0]["request_id"]
    assert request_id
    assert responses == []

    rendezvous.respond({"request_id": request_id, "result": {"txid": "abc"}})

    assert responses == [{"jsonrpc": "2.0", "id": 17, "result": {"txid": "abc"}}]


def test_rendezvous_assigns_distinct_requests_to_multiple_waiters() -> None:
    rendezvous = PeerSwapRendezvous()
    first = FakeRequest()
    second = FakeRequest()
    responses: list[dict[str, Any]] = []

    rendezvous.wait(cast(Request, first))
    rendezvous.wait(cast(Request, second))
    rendezvous.submit("txprepare", {"outputs": []}, 1, responses.append)
    rendezvous.submit("txsend", {"txid": "11" * 32}, 2, responses.append)

    assert first.results[0]["request_id"] != second.results[0]["request_id"]
    assert first.results[0]["method"] == "txprepare"
    assert second.results[0]["method"] == "txsend"


def test_rendezvous_queues_peer_swap_request_until_waiter_exists() -> None:
    rendezvous = PeerSwapRendezvous()
    waiter = FakeRequest()
    responses: list[dict[str, Any]] = []

    rendezvous.submit("txsend", {"txid": "11" * 32}, 23, responses.append)
    assert waiter.results == []

    rendezvous.wait(cast(Request, waiter))
    assert waiter.results[0]["method"] == "txsend"


def test_rendezvous_rejects_response_without_exactly_one_result_or_error() -> None:
    rendezvous = PeerSwapRendezvous()
    waiter = FakeRequest()
    rendezvous.wait(cast(Request, waiter))
    rendezvous.submit("txdiscard", {"txid": "22" * 32}, 31, lambda _: None)
    request_id = waiter.results[0]["request_id"]

    try:
        rendezvous.respond({"request_id": request_id})
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("Expected response validation error")


def test_rendezvous_preserves_string_and_zero_peer_swap_ids() -> None:
    rendezvous = PeerSwapRendezvous()
    first = FakeRequest()
    second = FakeRequest()
    responses: list[dict[str, Any]] = []

    rendezvous.wait(cast(Request, first))
    rendezvous.wait(cast(Request, second))
    rendezvous.submit("txprepare", {}, "peer-1", responses.append)
    rendezvous.submit("txsend", {"txid": "11" * 32}, 0, responses.append)

    first_request_id = first.results[0]["request_id"]
    second_request_id = second.results[0]["request_id"]
    rendezvous.respond(
        {
            "request_id": second_request_id,
            "error": {"code": -32010, "message": "failed"},
        }
    )
    rendezvous.respond({"request_id": first_request_id, "result": {"psbt": "abc"}})

    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "error": {"code": -32010, "message": "failed"},
        },
        {"jsonrpc": "2.0", "id": "peer-1", "result": {"psbt": "abc"}},
    ]


def test_rendezvous_matches_simultaneous_requests_out_of_order() -> None:
    rendezvous = PeerSwapRendezvous()
    waiters = [FakeRequest(), FakeRequest(), FakeRequest()]
    responses: list[dict[str, Any]] = []

    for waiter in waiters:
        rendezvous.wait(cast(Request, waiter))

    rendezvous.submit("txprepare", {"n": 1}, 101, responses.append)
    rendezvous.submit("txprepare", {"n": 2}, 102, responses.append)
    rendezvous.submit("txsend", {"n": 3}, 103, responses.append)

    request_ids = [waiter.results[0]["request_id"] for waiter in waiters]
    rendezvous.respond({"request_id": request_ids[2], "result": {"n": 3}})
    rendezvous.respond({"request_id": request_ids[0], "result": {"n": 1}})
    rendezvous.respond({"request_id": request_ids[1], "result": {"n": 2}})

    assert [response["id"] for response in responses] == [103, 101, 102]


def test_rendezvous_times_out_unassigned_request() -> None:
    rendezvous = PeerSwapRendezvous(request_timeout=0.01)
    responses: list[dict[str, Any]] = []

    rendezvous.submit("txprepare", {}, 17, responses.append)

    deadline = time.monotonic() + 1
    while not responses and time.monotonic() < deadline:
        time.sleep(0.005)

    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 17,
            "error": {
                "code": -32001,
                "message": "PeerSwap rendezvous request timed out",
            },
        }
    ]
    follow_up = FakeRequest()
    rendezvous.wait(cast(Request, follow_up))
    rendezvous.submit("txsend", {"txid": "11" * 32}, 18, responses.append)

    assert follow_up.results[0]["method"] == "txsend"


def test_rendezvous_drops_disconnected_response_without_leaking_state() -> None:
    rendezvous = PeerSwapRendezvous()
    waiter = FakeRequest()

    rendezvous.wait(cast(Request, waiter))

    def disconnected(_: dict[str, Any]) -> None:
        raise BrokenPipeError()

    rendezvous.submit("txprepare", {}, 17, disconnected)
    request_id = waiter.results[0]["request_id"]

    rendezvous.respond({"request_id": request_id, "result": {"psbt": "abc"}})

    assert rendezvous._requests == {}


def test_rendezvous_timeout_stops_after_assignment() -> None:
    rendezvous = PeerSwapRendezvous(request_timeout=0.01)
    waiter = FakeRequest()
    cancel_waiter = FakeRequest()
    responses: list[dict[str, Any]] = []

    rendezvous.wait(cast(Request, waiter))
    rendezvous.submit("txprepare", {}, 17, responses.append)
    request_id = waiter.results[0]["request_id"]

    cancel_waiter.params = {"request_id": request_id}
    rendezvous.wait_cancel(cast(Request, cancel_waiter))

    time.sleep(0.05)
    assert cancel_waiter.results == []
    assert responses == []
    assert request_id in rendezvous._requests

    rendezvous.cancel(request_id)
    assert cancel_waiter.results == [{"request_id": request_id, "state": "cancelled"}]
    assert rendezvous._requests == {}


def test_rendezvous_disconnect_cancels_assigned_operation() -> None:
    rendezvous = PeerSwapRendezvous()
    waiter = FakeRequest()
    cancel_waiter = FakeRequest()
    responses: list[dict[str, Any]] = []

    rendezvous.wait(cast(Request, waiter))
    rendezvous.submit("txsend", {}, 17, responses.append)
    request_id = waiter.results[0]["request_id"]

    cancel_waiter.params = {"request_id": request_id}
    rendezvous.wait_cancel(cast(Request, cancel_waiter))
    rendezvous.cancel(request_id)

    assert cancel_waiter.results == [{"request_id": request_id, "state": "cancelled"}]
    assert responses == []
    assert rendezvous._requests == {}

    with pytest.raises(ValueError, match="Unknown PeerSwap rendezvous request"):
        rendezvous.respond({"request_id": request_id, "result": {}})
