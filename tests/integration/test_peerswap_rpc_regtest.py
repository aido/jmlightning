from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import IntEnum
from pathlib import Path
from typing import Any

import pytest
from pyln.client import LightningRpc, RpcError

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.operations.open_channel import OpenChannelOperation
from jmlightning.operations.peerswap import (
    PeerSwapOperationDispatcher,
    PeerSwapPrepareTxOperation,
    PeerSwapPrepareTxRequest,
    PeerSwapRendezvousClient,
)

from .helpers import assert_channel_normal, bitcoin_cli, lightning_rpc, prepare_regtest

pytestmark = pytest.mark.anyio


class PeerSwapAssetType(IntEnum):
    """PeerSwap peerswaprpc.AssetType values."""

    UNSPECIFIED = 0
    BTC = 1
    LBTC = 2


class PeerSwapOperationType(IntEnum):
    """PeerSwap peerswaprpc.OperationType values."""

    UNSPECIFIED = 0
    SWAP_IN = 1
    SWAP_OUT = 2


PEERSWAP_TERMINAL_STATES = {
    "State_ClaimedCsv",
    "State_ClaimedPreimage",
    "State_ClaimedCoop",
    "State_SwapCanceled",
}


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"regtest integration environment variable {name} is not set")
    return value


def _policy(rpc: LightningRpc) -> dict[str, Any]:
    result = rpc.call("peerswap-reloadpolicy", {})
    assert isinstance(result, dict)
    return result


def _ensure_peer_allowlisted(rpc: LightningRpc, peer_id: str) -> None:
    policy = _policy(rpc)
    if peer_id in policy.get("allowlisted_peers", []):
        return

    try:
        rpc.call("peerswap-addpeer", {"peer_pubkey": peer_id})
    except RpcError as exc:
        # jm-peerswap can report an already-whitelisted peer even when the
        # immediately preceding reloadpolicy response has not reflected that
        # state yet.  Adding a peer is idempotent for this test's purpose, so
        # treat that race as success but preserve all other RPC failures.
        if "peer is already whitelisted" not in str(exc):
            raise


def _start_peerswap_rendezvous(
    context: dict[str, Any],
) -> list[PeerSwapRendezvousClient]:
    """Start rendezvous consumers for both CLN nodes used by PeerSwap."""
    clients = []
    for socket in (context["cln_socket"], context["peer_socket"]):
        operation = PeerSwapPrepareTxOperation(
            config=context["config"],
            cln_socket=Path(socket),
        )
        clients.append(
            PeerSwapRendezvousClient(
                cln_socket=Path(socket),
                handler=PeerSwapOperationDispatcher(operation),
            )
        )

    for client in clients:
        client.start()

    return clients


def _stop_peerswap_rendezvous(clients: list[PeerSwapRendezvousClient]) -> None:
    for client in reversed(clients):
        client.stop()


def _wait_for_swap_terminal(
    rpc: LightningRpc,
    swap_id: str,
    timeout: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = rpc.call("peerswap-getswap", {"swap_id": swap_id})
        assert isinstance(result, dict)
        state = result.get("current")
        if state in PEERSWAP_TERMINAL_STATES:
            return result
        time.sleep(1)
    pytest.fail(f"PeerSwap swap {swap_id} did not reach a terminal state")


def _fund_cln_wallet(
    rpc: LightningRpc,
    bitcoin_datadir: Path,
    amount_btc: float,
) -> None:
    address = rpc.call("newaddr")["bech32"]
    txid = bitcoin_cli(
        bitcoin_datadir,
        "-rpcwallet=ci",
        "sendtoaddress",
        address,
        str(amount_btc),
    )
    assert isinstance(txid, str) and len(txid) == 64
    mining_address = str(bitcoin_cli(bitcoin_datadir, "-rpcwallet=ci", "getnewaddress"))
    bitcoin_cli(
        bitcoin_datadir,
        "-rpcwallet=ci",
        "generatetoaddress",
        "6",
        mining_address,
    )

    # PeerSwap reads the CLN wallet through listfunds. Wait for the newly
    # confirmed output to become visible before starting the swap RPC: the
    # PeerSwap RPC itself has a shorter timeout than a wallet rescan can take
    # on CI.
    _wait_for_onchain_balance(rpc, int(amount_btc * 100_000_000))


def _wait_for_onchain_balance(rpc: LightningRpc, minimum_sat: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        funds = rpc.call("listfunds")
        outputs = funds.get("outputs", [])
        balance = sum(
            int(output.get("amount_msat", 0)) // 1000
            for output in outputs
            if isinstance(output, dict)
            and output.get("status") == "confirmed"
            and output.get("reserved") is not True
        )
        if balance >= minimum_sat:
            return
        time.sleep(1)
    pytest.fail(f"CLN wallet did not reach {minimum_sat} sat onchain balance")


def _set_peer_btc_premiums(
    rpc: LightningRpc, peer_id: str, premium_rate_ppm: int
) -> None:
    for operation in ("swap_in", "swap_out"):
        result = rpc.call(
            "peerswap-updatepremiumrate",
            {
                "peer_id": peer_id,
                "asset": "btc",
                "operation": operation,
                "premium_rate_ppm": premium_rate_ppm,
            },
        )
        assert result["asset"] == 1
        # PeerSwap uses omitempty for premium_rate_ppm, so a zero
        # premium is omitted from the JSON-RPC result.
        assert result.get("premium_rate_ppm", 0) == premium_rate_ppm


def _wait_for_peer_btc_premiums(
    rpc: LightningRpc,
    peer_id: str,
    premium_rate_ppm: int,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = rpc.call("peerswap-listpeers", {})
        assert isinstance(result, list)
        for peer in result:
            if not isinstance(peer, dict) or peer.get("node_id") != peer_id:
                continue
            peer_premium = peer.get("peer_premium")
            if not isinstance(peer_premium, dict):
                continue
            rates = peer_premium.get("rates", [])
            btc_rates = {
                rate.get("operation"): rate.get("premium_rate_ppm", 0)
                for rate in rates
                if isinstance(rate, dict) and rate.get("asset") == 1
            }
            if all(
                btc_rates.get(operation) == premium_rate_ppm for operation in (1, 2)
            ):
                return
        time.sleep(1)
    pytest.fail(
        f"PeerSwap capability for {peer_id} did not advertise "
        f"BTC premiums at {premium_rate_ppm} ppm"
    )


@asynccontextmanager
async def _peer_swap_rendezvous(
    context: dict[str, Any],
    cln_socket: str,
) -> AsyncIterator[None]:
    operation = PeerSwapPrepareTxOperation(
        config=context["config"],
        cln_socket=Path(cln_socket),
    )
    await operation.connect()
    client = PeerSwapRendezvousClient(
        Path(cln_socket),
        PeerSwapOperationDispatcher(operation),
        pool_size=1,
    )
    client.start()
    try:
        yield
    finally:
        client.stop()
        await operation.close()


def _mine_regtest_blocks(bitcoin_datadir: Path, blocks: int = 6) -> None:
    mining_address = str(bitcoin_cli(bitcoin_datadir, "-rpcwallet=ci", "getnewaddress"))
    bitcoin_cli(
        bitcoin_datadir,
        "-rpcwallet=ci",
        "generatetoaddress",
        str(blocks),
        mining_address,
    )


def _short_channel_id(rpc: LightningRpc, peer_id: str, channel_id: str) -> str:
    channels = rpc.listpeerchannels(peer_id).get("channels", [])
    matching = [
        channel
        for channel in channels
        if isinstance(channel, dict)
        and channel.get("peer_id") == peer_id
        and channel.get("channel_id") == channel_id
        and isinstance(channel.get("short_channel_id"), str)
    ]
    assert matching, f"CLN has no short channel id for channel {channel_id}"
    return str(matching[0]["short_channel_id"])


async def _setup_peer_swap_channel(tmp_path: Path) -> dict[str, Any]:
    context = await prepare_regtest(tmp_path)
    peer1 = lightning_rpc(context["cln_socket"])
    peer2 = lightning_rpc(context["peer_socket"])
    peer1_node_id = str(peer1.getinfo()["id"])
    peer2_node_id = str(peer2.getinfo()["id"])

    config = context["config"].model_copy(update={"amount": 300_000})
    previous_channel_ids = {
        channel["channel_id"]
        for channel in peer1.listpeerchannels(peer2_node_id).get("channels", [])
        if isinstance(channel, dict) and isinstance(channel.get("channel_id"), str)
    }
    operation = OpenChannelOperation(
        config=config,
        cln_socket=Path(context["cln_socket"]),
    )
    await operation.execute(peer2_node_id)
    channel_id = assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=peer2_node_id,
        previous_channel_ids=previous_channel_ids,
    )

    return {
        **context,
        "peer1": peer1,
        "peer2": peer2,
        "peer1_node_id": peer1_node_id,
        "peer2_node_id": peer2_node_id,
        "channel_id": channel_id,
        "scid": _short_channel_id(peer1, peer2_node_id, channel_id),
    }


async def test_peerswap_btc_rpc_surface_regtest(tmp_path: Path) -> None:
    context = await _setup_peer_swap_channel(tmp_path)
    peer1: LightningRpc = context["peer1"]
    peer2: LightningRpc = context["peer2"]
    peer1_node_id = str(context["peer1_node_id"])
    peer2_node_id = str(context["peer2_node_id"])

    # The following calls cover every BTC-relevant RPC advertised by
    # PeerSwap No policy state is edited outside the bridge.
    # Make the allowlist deterministic through the bridge and deliberately
    # exercise both removepeer and addpeer even when CI preconfigured them.
    for rpc, peer_id in (
        (peer1, peer2_node_id),
        (peer2, peer1_node_id),
    ):
        if peer_id in _policy(rpc).get("allowlisted_peers", []):
            rpc.call("peerswap-removepeer", {"peer_pubkey": peer_id})
        result = rpc.call("peerswap-addpeer", {"peer_pubkey": peer_id})
        assert peer_id in result["allowlisted_peers"]

    result = peer1.call("peerswap-listpeers", {})
    assert isinstance(result, list)
    assert any(peer.get("node_id") == peer2_node_id for peer in result)
    assert isinstance(peer1.call("peerswap-listconfig", {}), dict)
    assert isinstance(peer1.call("peerswap-listswaps", {}), dict)
    assert isinstance(peer1.call("peerswap-listactiveswaps", {}), dict)
    assert isinstance(peer1.call("peerswap-listswaprequests", {}), list)
    assert isinstance(_policy(peer1), dict)

    result = peer1.call("peerswap-allowswaprequests", {"allow_swap_requests": "0"})
    assert result["allowlisted_peers"]
    result = peer1.call("peerswap-allowswaprequests", {"allow_swap_requests": "1"})
    assert result["allowlisted_peers"]

    result = peer1.call("peerswap-addsuspeer", {"peer_pubkey": peer2_node_id})
    assert peer2_node_id in result["suspicious_peers"]
    result = peer1.call("peerswap-removesuspeer", {"peer_pubkey": peer2_node_id})
    assert peer2_node_id not in result["suspicious_peers"]

    result = peer1.call(
        "peerswap-updatepremiumrate",
        {
            "peer_id": peer2_node_id,
            "asset": "btc",
            "operation": "swap_out",
            "premium_rate_ppm": 123,
        },
    )
    assert result["asset"] == int(PeerSwapAssetType.BTC)
    assert result["operation"] == int(PeerSwapOperationType.SWAP_OUT)

    result = peer1.call(
        "peerswap-getpremiumrate",
        {
            "peer_id": peer2_node_id,
            "asset": "btc",
            "operation": "swap_out",
        },
    )
    assert result["asset"] == int(PeerSwapAssetType.BTC)
    assert result["operation"] == int(PeerSwapOperationType.SWAP_OUT)
    assert result["premium_rate_ppm"] == 123

    result = peer1.call(
        "peerswap-deletepremiumrate",
        {
            "peer_id": peer2_node_id,
            "asset": "btc",
            "operation": "swap_out",
        },
    )
    assert result["asset"] == int(PeerSwapAssetType.BTC)
    assert result["operation"] == int(PeerSwapOperationType.SWAP_OUT)

    result = peer1.call(
        "peerswap-updateglobalpremiumrate",
        {"asset": "btc", "operation": "swap_in", "premium_rate_ppm": 123},
    )
    assert result["asset"] == int(PeerSwapAssetType.BTC)
    assert result["operation"] == int(PeerSwapOperationType.SWAP_IN)
    assert result["premium_rate_ppm"] == 123

    result = peer1.call(
        "peerswap-getglobalpremiumrate",
        {"asset": "btc", "operation": "swap_in"},
    )
    assert result["asset"] == int(PeerSwapAssetType.BTC)
    assert result["operation"] == int(PeerSwapOperationType.SWAP_IN)
    assert result["premium_rate_ppm"] == 123

    # Exercise both sides of allowlist management through the jm-peerswap
    # bridge and restore the state for subsequent integration tests.
    result = peer1.call("peerswap-removepeer", {"peer_pubkey": peer2_node_id})
    assert peer2_node_id not in result["allowlisted_peers"]
    result = peer2.call("peerswap-removepeer", {"peer_pubkey": peer1_node_id})
    assert peer1_node_id not in result["allowlisted_peers"]
    peer1.call("peerswap-addpeer", {"peer_pubkey": peer2_node_id})
    peer2.call("peerswap-addpeer", {"peer_pubkey": peer1_node_id})


async def test_peerswap_btc_swap_in_and_out_complete_lifecycle(tmp_path: Path) -> None:
    context = await _setup_peer_swap_channel(tmp_path)
    peer1: LightningRpc = context["peer1"]
    peer2: LightningRpc = context["peer2"]
    peer1_node_id = str(context["peer1_node_id"])
    peer2_node_id = str(context["peer2_node_id"])
    scid = str(context["scid"])

    _ensure_peer_allowlisted(peer1, peer2_node_id)
    _ensure_peer_allowlisted(peer2, peer1_node_id)

    # Configure peer2's BTC premiums for peer1 through the jm-peerswap RPC
    # path. Peer2 is the responding side for both swaps below, so both BTC
    # operation rates must be zero. Wait for peer1 to receive the capability
    # update before starting the protocol.
    _set_peer_btc_premiums(peer2, peer1_node_id, 0)
    _wait_for_peer_btc_premiums(peer1, peer2_node_id, 0)

    # Peer2 supplies the onchain side of the swap-out. The test never edits
    # PeerSwap's database or policy file directly.
    _fund_cln_wallet(peer2, context["bitcoin_datadir"], 0.1)

    # jm-peerswap queues txprepare/txsend/txdiscard at the bridge boundary.
    # Keep rendezvous consumers running on both CLN nodes for the complete
    # lifetime of the PeerSwap calls rather than relying on swap timeouts.
    rendezvous_clients = _start_peerswap_rendezvous(context)
    try:
        swap_out = peer1.call(
            "peerswap-swap-out",
            {
                "short_channel_id": scid,
                "amt_sat": 100_000,
                "asset": "btc",
                "premium_rate_limit_ppm": 0,
                "force": False,
            },
        )
        assert isinstance(swap_out, dict)
        swap_out_id = swap_out["id"]
        assert isinstance(swap_out_id, str) and swap_out_id

        # PeerSwap waits for three Bitcoin confirmations of the
        # opening transaction before the swap-out sender can continue to
        # claim the swap. Regtest has no background miner, so mine the
        # confirmations explicitly.
        _mine_regtest_blocks(context["bitcoin_datadir"], blocks=3)

        terminal_out = _wait_for_swap_terminal(peer1, swap_out_id)
        assert terminal_out["swap_id"] == swap_out_id

        # getswap returns PeerSwap's internal serialized state machine. The
        # human-readable BTC/type fields are exposed by listswaps.
        peer1_swaps = peer1.call("peerswap-listswaps", {})
        swap_out_record = next(
            swap for swap in peer1_swaps["swaps"] if swap.get("id") == swap_out_id
        )
        assert swap_out_record["asset"] == "btc"
        assert swap_out_record["type"] == "swap-out"
        assert isinstance(
            peer1.call("peerswap-getswap", {"swap_id": swap_out_id}), dict
        )

        _mine_regtest_blocks(context["bitcoin_datadir"])

        # The swap-out claim returns the opening output to peer1, but the
        # claim transaction itself pays a miner fee.  The subsequent swap-in
        # also needs the opening transaction fee, so the claimed 100,000-sat
        # amount alone is not sufficient to fund another 100,000-sat swap.
        # Give peer1 a small independent onchain reserve for the reverse leg.
        _wait_for_onchain_balance(peer1, 1)
        _fund_cln_wallet(peer1, context["bitcoin_datadir"], 0.01)

        swap_in = peer1.call(
            "peerswap-swap-in",
            {
                "short_channel_id": scid,
                "amt_sat": 100_000,
                "asset": "btc",
                "premium_limit_ppm": 0,
                "force": False,
            },
        )
        assert isinstance(swap_in, dict)
        swap_in_id = swap_in["id"]
        assert isinstance(swap_in_id, str) and swap_in_id

        # The swap-in receiver also waits for three Bitcoin confirmations
        # before paying the claim invoice. Mine those confirmations so the
        # sender can reach its terminal claimed state.
        _mine_regtest_blocks(context["bitcoin_datadir"], blocks=3)

        terminal_in = _wait_for_swap_terminal(peer1, swap_in_id)
        assert terminal_in["swap_id"] == swap_in_id

        # Explicitly exercise the corresponding receiver-side historical view.
        peer1_swaps = peer1.call("peerswap-listswaps", {})
        swap_in_record = next(
            swap for swap in peer1_swaps["swaps"] if swap.get("id") == swap_in_id
        )
        assert swap_in_record["asset"] == "btc"
        assert swap_in_record["type"] == "swap-in"
        peer2_swaps = peer2.call("peerswap-listswaps", {})
        assert any(swap.get("id") == swap_out_id for swap in peer1_swaps["swaps"])
        assert any(swap.get("id") == swap_out_id for swap in peer2_swaps["swaps"])

        # Leave the long-lived CI nodes allowlisted for any later test.
        _ensure_peer_allowlisted(peer1, peer2_node_id)
        _ensure_peer_allowlisted(peer2, peer1_node_id)
    finally:
        _stop_peerswap_rendezvous(rendezvous_clients)


async def test_peerswap_txdiscard_releases_real_joinmarket_utxo(tmp_path: Path) -> None:
    """txprepare -> txdiscard must release the real JoinMarket wallet input."""
    context = await prepare_regtest(tmp_path)
    peer = lightning_rpc(context["cln_socket"])
    address = str(peer.call("newaddr")["bech32"])

    operation = PeerSwapPrepareTxOperation(
        config=context["config"],
        cln_socket=Path(context["cln_socket"]),
    )
    await operation.connect()
    try:
        prepared = await operation.execute(
            PeerSwapPrepareTxRequest.from_rpc(
                {
                    "outputs": [{address: 100_000}],
                    "feerate": "urgent",
                    "minconf": 1,
                    "utxos": [],
                }
            )
        )
        locked = {(coin.utxo.txid, coin.utxo.vout) for coin in prepared.locked}
        assert locked

        # The prepared operation must hide its own locked inputs from a later
        # funding selection while the PeerSwap transaction is outstanding.
        selected_while_prepared = {
            (coin.utxo.txid, coin.utxo.vout)
            for coin in prepared.adapter.get_utxos(context["config"].mixdepth)
        }
        assert locked.isdisjoint(selected_while_prepared)

        discarded = await operation.discard(prepared.txid)
        assert discarded["txid"] == prepared.txid
        assert operation._prepared == {}

        # Reopen the real JoinMarket wallet and verify the same UTXO is once
        # again selectable. This catches failures where the in-memory state is
        # dropped but the persistent JoinMarket freeze/metadata lock remains.
        adapter = JoinMarketAdapter(context["config"])
        await adapter.connect()
        try:
            available = {
                (coin.utxo.txid, coin.utxo.vout)
                for coin in adapter.get_utxos(context["config"].mixdepth)
            }
            assert locked <= available
        finally:
            await adapter.close()
    finally:
        await operation.close()
