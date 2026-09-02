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
from jmlightning.operations.open_channel import OpenChannelOperation

pytestmark = pytest.mark.anyio

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"regtest integration environment variable {name} is not set")
    return value


def _bitcoin_cli(datadir: Path, *args: str) -> Any:
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


def _lightning_rpc(socket: str) -> LightningRpc:
    return LightningRpc(socket)


async def _prepare_regtest(tmp_path: Path) -> dict[str, Any]:
    bitcoin_datadir = Path(_required_env("BITCOIN_DATADIR"))
    cln_socket = _required_env("CLN_RPC_SOCKET")
    peer_id = _required_env("CLN_PEER_ID")
    peer_socket = _required_env("CLN_PEER_RPC_SOCKET")
    rpc_url = _required_env("BITCOIN_RPC_URL")

    data_dir = tmp_path / "joinmarket"
    data_dir.mkdir()
    mnemonic_file = tmp_path / "default.mnemonic"
    mnemonic_file.write_text(TEST_MNEMONIC + "\n", encoding="utf-8")

    current_height = int(_bitcoin_cli(bitcoin_datadir, "getblockcount"))

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
        _bitcoin_cli(
            bitcoin_datadir,
            "-rpcwallet=ci",
            "sendtoaddress",
            funding_source_address,
            "0.5",
        )
    )
    mining_address = str(
        _bitcoin_cli(bitcoin_datadir, "-rpcwallet=ci", "getnewaddress")
    )
    _bitcoin_cli(
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


def _run_cli(
    *,
    data_dir: Path,
    mnemonic_file: Path,
    cln_socket: str,
    peer_id: str,
    rpc_url: str,
    confirm: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (
        "JOINMARKET_CONFIG_FILE",
        "MNEMONIC",
        "MNEMONIC_FILE",
        "MNEMONIC_PASSWORD",
    ):
        env.pop(name, None)
    env.update(
        {
            "NETWORK_CONFIG__NETWORK": "regtest",
            "NETWORK_CONFIG__BITCOIN_NETWORK": "regtest",
            "BITCOIN__BACKEND_TYPE": "descriptor_wallet",
            "BITCOIN__RPC_URL": rpc_url,
            "BITCOIN__RPC_USER": "test",
            "BITCOIN__RPC_PASSWORD": "test",
            "WALLET__MIXDEPTH_COUNT": "5",
            "WALLET__GAP_LIMIT": "6",
            "WALLET__SCAN_RANGE": "100",
            "WALLET__MAX_SATS_FREEZE_REUSE": "-1",
            "WALLET__RECONSTRUCT_HISTORY": "false",
        }
    )

    command = [
        "jm-lightning",
        "open-channel",
        peer_id,
        "--amount",
        "100000",
        "--cln-socket",
        cln_socket,
        "--data-dir",
        str(data_dir),
        "--mnemonic-file",
        str(mnemonic_file),
    ]
    if not confirm:
        command.append("--yes")

    return subprocess.run(
        command,
        input="y\n" if confirm else None,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def _assert_cli_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"jm-lightning open-channel failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _assert_channel_normal(
    *,
    bitcoin_datadir: Path,
    cln_socket: str,
    peer_socket: str,
    peer_id: str,
) -> str:
    source_rpc = _lightning_rpc(cln_socket)
    peer_rpc = _lightning_rpc(peer_socket)

    peer_channels = source_rpc.listpeerchannels(peer_id)
    channels = peer_channels.get("channels", [])
    matching = [
        channel
        for channel in channels
        if isinstance(channel, dict) and channel.get("peer_id") == peer_id
    ]
    assert matching, f"CLN has no channel for peer {peer_id}"

    channel = matching[0]
    funding_txid = channel.get("funding_txid")
    assert isinstance(funding_txid, str) and len(funding_txid) == 64
    assert channel.get("funding", {}).get("withheld") is not True

    tx = _bitcoin_cli(bitcoin_datadir, "getrawtransaction", funding_txid, "true")
    assert tx["txid"] == funding_txid

    neutral_address = str(
        _bitcoin_cli(bitcoin_datadir, "-rpcwallet=ci", "getnewaddress")
    )
    _bitcoin_cli(
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
            if isinstance(item, dict) and item.get("funding_txid") == funding_txid
        ]
        if matching and matching[0].get("state") == "CHANNELD_NORMAL":
            break
        time.sleep(1)
    else:
        pytest.fail(f"channel {funding_txid} did not reach CHANNELD_NORMAL")

    peer_info = peer_rpc.getinfo()
    assert peer_info["id"] == peer_id
    return funding_txid


async def test_open_channel_happy_path(tmp_path: Path) -> None:
    context = await _prepare_regtest(tmp_path)

    operation = OpenChannelOperation(
        config=context["config"],
        cln_socket=Path(context["cln_socket"]),
    )
    await operation.execute(context["peer_id"])

    _assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
    )


async def test_open_channel_cli_happy_path(tmp_path: Path) -> None:
    context = await _prepare_regtest(tmp_path)

    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        peer_id=context["peer_id"],
        rpc_url=context["rpc_url"],
    )
    _assert_cli_success(result)

    _assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
    )


async def test_open_channel_cli_happy_path_with_confirmation(
    tmp_path: Path,
) -> None:
    context = await _prepare_regtest(tmp_path)

    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        peer_id=context["peer_id"],
        rpc_url=context["rpc_url"],
        confirm=True,
    )
    _assert_cli_success(result)

    _assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
    )
