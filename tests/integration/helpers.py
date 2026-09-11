from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from jmcore.models import NetworkType
from pydantic import SecretStr
from pyln.client import LightningRpc

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.config import CLNConfig
from jmlightning.lightning.backend import FeePriority

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"regtest integration environment variable {name} is not set")
    return value


def assert_channel_normal(
    *,
    bitcoin_datadir: Path,
    cln_socket: str,
    peer_socket: str,
    peer_id: str,
    previous_channel_ids: set[str] | None = None,
) -> str:
    source_rpc = lightning_rpc(cln_socket)
    peer_rpc = lightning_rpc(peer_socket)

    peer_channels = source_rpc.listpeerchannels(peer_id)
    channels = peer_channels.get("channels", [])
    matching = [
        channel
        for channel in channels
        if isinstance(channel, dict)
        and channel.get("peer_id") == peer_id
        and (
            previous_channel_ids is None
            or channel.get("channel_id") not in previous_channel_ids
        )
    ]
    assert matching, f"CLN has no matching channel for peer {peer_id}"
    if previous_channel_ids is not None:
        assert len(matching) == 1, (
            f"CLN has multiple new channels for peer {peer_id}: "
            f"{[channel.get('channel_id') for channel in matching]}"
        )
    channel = matching[0]
    channel_id = channel.get("channel_id")
    assert isinstance(channel_id, str) and channel_id
    funding_txid = channel.get("funding_txid")
    assert isinstance(funding_txid, str) and len(funding_txid) == 64
    assert channel.get("funding", {}).get("withheld") is not True

    tx = bitcoin_cli(bitcoin_datadir, "getrawtransaction", funding_txid, "true")
    assert tx["txid"] == funding_txid

    neutral_address = str(
        bitcoin_cli(bitcoin_datadir, "-rpcwallet=ci", "getnewaddress")
    )
    bitcoin_cli(
        bitcoin_datadir,
        "-rpcwallet=ci",
        "generatetoaddress",
        "6",
        neutral_address,
    )

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        channels = source_rpc.listpeerchannels(peer_id).get("channels", [])
        matching = [
            item
            for item in channels
            if isinstance(item, dict) and item.get("channel_id") == channel_id
        ]
        if matching and matching[0].get("state") == "CHANNELD_NORMAL":
            break
        time.sleep(1)
    else:
        pytest.fail(f"channel {channel_id} did not reach CHANNELD_NORMAL")

    peer_info = peer_rpc.getinfo()
    assert peer_info["id"] == peer_id
    return channel_id


def bitcoin_cli(datadir: Path, *args: str) -> Any:
    rpc_user = os.environ.get("BITCOIN_RPC_USER")
    rpc_password = os.environ.get("BITCOIN_RPC_PASSWORD")

    command = [
        "bitcoin-cli",
        f"-datadir={datadir}",
        "-regtest",
    ]

    if rpc_user is not None:
        command.append(f"-rpcuser={rpc_user}")
    if rpc_password is not None:
        command.append(f"-rpcpassword={rpc_password}")

    command.extend(args)

    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def lightning_rpc(socket: str) -> LightningRpc:
    return LightningRpc(socket)


async def prepare_regtest(tmp_path: Path) -> dict[str, Any]:
    bitcoin_datadir = Path(_required_env("BITCOIN_DATADIR"))
    cln_socket = _required_env("CLN_RPC_SOCKET")
    peer_id = _required_env("CLN_PEER_ID")
    peer_socket = _required_env("CLN_PEER_RPC_SOCKET")
    rpc_url = _required_env("BITCOIN_RPC_URL")

    data_dir = tmp_path / "joinmarket"
    data_dir.mkdir()
    mnemonic_file = tmp_path / "default.mnemonic"
    mnemonic_file.write_text(TEST_MNEMONIC + "\n", encoding="utf-8")

    current_height = int(bitcoin_cli(bitcoin_datadir, "getblockcount"))

    config = CLNConfig(
        mnemonic=SecretStr(TEST_MNEMONIC),
        passphrase=SecretStr(""),
        network=NetworkType.REGTEST,
        bitcoin_network=NetworkType.REGTEST,
        data_dir=data_dir,
        backend_type="descriptor_wallet",
        backend_config={
            "rpc_url": rpc_url,
            "rpc_user": "test",
            "rpc_password": "test",
        },
        creation_height=current_height,
        mixdepth_count=5,
        gap_limit=6,
        scan_range=100,
        max_sats_freeze_reuse=-1,
        reconstruct_history=False,
        amount=100_000,
        mixdepth=0,
        announce=False,
        fee_priority=FeePriority.NORMAL,
    )

    adapter = JoinMarketAdapter(config)
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        funding_source_address = wallet.get_receive_address(0, 0)
    finally:
        await adapter.close()

    source_txid = str(
        bitcoin_cli(
            bitcoin_datadir,
            "-rpcwallet=ci",
            "sendtoaddress",
            funding_source_address,
            "0.5",
        )
    )
    mining_address = str(bitcoin_cli(bitcoin_datadir, "-rpcwallet=ci", "getnewaddress"))
    bitcoin_cli(
        bitcoin_datadir,
        "-rpcwallet=ci",
        "generatetoaddress",
        "1",
        mining_address,
    )

    adapter = JoinMarketAdapter(config)
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        await wallet.sync_all()
        utxos = wallet.utxo_cache.get(0, [])
        source_utxo = next(
            (utxo for utxo in utxos if utxo.txid == source_txid),
            None,
        )
        assert source_utxo is not None, (
            f"funding UTXO {source_txid} not found in JoinMarket wallet"
        )
        assert wallet.metadata_store is not None

        # The real policy requires exact CoinJoin provenance. This happy-path
        # fixture records the same two pieces of state that JoinMarket's normal
        # CoinJoin history supplies.
        wallet.metadata_store.mark_coinjoin_outputs([source_utxo.outpoint])
        wallet.metadata_store.mark_address_used(
            source_utxo.address,
            origin="cj_out",
        )
    finally:
        await adapter.close()

    return {
        "bitcoin_datadir": bitcoin_datadir,
        "cln_socket": cln_socket,
        "peer_id": peer_id,
        "peer_socket": peer_socket,
        "rpc_url": rpc_url,
        "data_dir": data_dir,
        "mnemonic_file": mnemonic_file,
        "config": config,
    }
