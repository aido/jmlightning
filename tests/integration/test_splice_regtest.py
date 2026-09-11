from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.operations.open_channel import OpenChannelOperation
from jmlightning.operations.splice import SpliceOperation

from .helpers import (
    assert_channel_normal,
    bitcoin_cli,
    lightning_rpc,
    prepare_regtest,
)

pytestmark = pytest.mark.anyio


async def _prepare_splice_regtest(tmp_path: Path) -> dict[str, Any]:
    return await prepare_regtest(tmp_path)


async def _prepare_splice_utxo(context: dict[str, Any]) -> None:
    adapter = JoinMarketAdapter(context["config"])
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        splice_address = await wallet.get_new_address_verified(0)
    finally:
        await adapter.close()

    source_txid = str(
        bitcoin_cli(
            context["bitcoin_datadir"],
            "-rpcwallet=ci",
            "sendtoaddress",
            splice_address,
            "0.2",
        )
    )
    mining_address = str(
        bitcoin_cli(
            context["bitcoin_datadir"],
            "-rpcwallet=ci",
            "getnewaddress",
        )
    )
    bitcoin_cli(
        context["bitcoin_datadir"],
        "-rpcwallet=ci",
        "generatetoaddress",
        "1",
        mining_address,
    )

    adapter = JoinMarketAdapter(context["config"])
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        await wallet.sync_all()
        utxos = wallet.utxo_cache.get(context["config"].mixdepth, [])
        splice_utxo = next(
            (utxo for utxo in utxos if utxo.txid == source_txid),
            None,
        )
        assert splice_utxo is not None, (
            f"splice UTXO {source_txid} not found in JoinMarket wallet"
        )
        assert wallet.metadata_store is not None

        # The real policy requires exact CoinJoin provenance. This happy-path
        # fixture records the same two pieces of state that JoinMarket's normal
        # CoinJoin history supplies.
        wallet.metadata_store.mark_coinjoin_outputs(
            [f"{splice_utxo.txid}:{splice_utxo.vout}"]
        )
        wallet.metadata_store.mark_address_used(
            splice_utxo.address,
            origin="cj_out",
        )

        # The channel-opening operation has already run, so it is now safe to
        # make the dedicated splice UTXO the only eligible funding coin. Keep
        # production coin selection unchanged while making this integration
        # fixture deterministic.
        available = adapter.get_utxos(context["config"].mixdepth)
        splice_outpoint = (splice_utxo.txid, splice_utxo.vout)
        for coin in available:
            if (coin.utxo.txid, coin.utxo.vout) != splice_outpoint:
                wallet.freeze_utxo(f"{coin.utxo.txid}:{coin.utxo.vout}")

        eligible = adapter.get_utxos(context["config"].mixdepth)
        assert [(coin.utxo.txid, coin.utxo.vout) for coin in eligible] == [
            splice_outpoint
        ], (
            "splice fixture must leave exactly one eligible UTXO: "
            f"{source_txid}:{splice_utxo.vout}"
        )
    finally:
        await adapter.close()

    context["splice_source_txid"] = source_txid
    context["splice_source_vout"] = splice_utxo.vout


def _channel_funding_txid(
    *,
    cln_socket: str,
    peer_id: str,
    channel_id: str,
) -> str:
    rpc = lightning_rpc(cln_socket)
    channels = rpc.listpeerchannels(peer_id).get("channels", [])
    matching = [
        channel
        for channel in channels
        if isinstance(channel, dict) and channel.get("channel_id") == channel_id
    ]
    assert matching, f"CLN has no channel {channel_id}"

    funding_txid = matching[0].get("funding_txid")
    assert isinstance(funding_txid, str) and len(funding_txid) == 64
    return funding_txid


def _assert_splice_channel_normal(
    *,
    bitcoin_datadir: Path,
    cln_socket: str,
    peer_socket: str,
    peer_id: str,
    channel_id: str,
    splice_txid: str,
) -> str:
    source_rpc = lightning_rpc(cln_socket)
    peer_rpc = lightning_rpc(peer_socket)

    tx = bitcoin_cli(bitcoin_datadir, "getrawtransaction", splice_txid, "true")
    assert tx["txid"] == splice_txid

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
        pytest.fail(
            f"channel {channel_id} did not reach CHANNELD_NORMAL "
            f"after splice {splice_txid}"
        )

    peer_info = peer_rpc.getinfo()
    assert peer_info["id"] == peer_id
    return splice_txid


def _assert_joinmarket_input(
    *,
    bitcoin_datadir: Path,
    txid: str,
    source_txid: str,
    source_vout: int,
) -> None:
    tx = bitcoin_cli(bitcoin_datadir, "getrawtransaction", txid, "true")
    inputs = tx.get("vin", [])
    assert any(
        isinstance(item, dict)
        and item.get("txid") == source_txid
        and item.get("vout") == source_vout
        for item in inputs
    ), f"splice transaction {txid} does not spend {source_txid}:{source_vout}"


async def _open_channel(context: dict[str, Any]) -> str:
    rpc = lightning_rpc(context["cln_socket"])
    channels = rpc.listpeerchannels(context["peer_id"]).get("channels", [])
    previous_channel_ids = {
        channel["channel_id"]
        for channel in channels
        if isinstance(channel, dict) and isinstance(channel.get("channel_id"), str)
    }

    operation = OpenChannelOperation(
        config=context["config"],
        cln_socket=Path(context["cln_socket"]),
    )
    await operation.execute(context["peer_id"])

    return assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
        previous_channel_ids=previous_channel_ids,
    )


def _run_cli(
    *,
    data_dir: Path,
    mnemonic_file: Path,
    cln_socket: str,
    channel_id: str,
    amount: int,
    rpc_url: str,
    confirm: bool = False,
) -> CompletedProcess[str]:
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
        "splice-in",
        channel_id,
        "--amount",
        str(amount),
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


def _assert_cli_success(result: CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"jm-lightning splice-in failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _splice_txid_from_cli(result: CompletedProcess[str]) -> str:
    match = re.search(
        r"Splice transaction:\s*([0-9a-f]{64})(?=\s|$)",
        result.stdout,
        re.MULTILINE,
    )
    assert match, f"CLI did not report a splice transaction id:\n{result.stdout}"
    return match.group(1)


async def test_splice_in_happy_path(tmp_path: Path) -> None:
    context = await _prepare_splice_regtest(tmp_path)
    channel_id = await _open_channel(context)
    await _prepare_splice_utxo(context)

    operation = SpliceOperation(
        config=context["config"],
        cln_socket=Path(context["cln_socket"]),
    )
    splice_txid = await operation.execute(channel_id)

    funding_txid = _assert_splice_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
        channel_id=channel_id,
        splice_txid=splice_txid,
    )
    _assert_joinmarket_input(
        bitcoin_datadir=context["bitcoin_datadir"],
        txid=funding_txid,
        source_txid=context["splice_source_txid"],
        source_vout=context["splice_source_vout"],
    )


async def test_splice_in_cli_happy_path(tmp_path: Path) -> None:
    context = await _prepare_splice_regtest(tmp_path)
    channel_id = await _open_channel(context)
    await _prepare_splice_utxo(context)
    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        channel_id=channel_id,
        amount=100_000,
        rpc_url=context["rpc_url"],
    )
    _assert_cli_success(result)
    splice_txid = _splice_txid_from_cli(result)

    funding_txid = _assert_splice_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
        channel_id=channel_id,
        splice_txid=splice_txid,
    )
    _assert_joinmarket_input(
        bitcoin_datadir=context["bitcoin_datadir"],
        txid=funding_txid,
        source_txid=context["splice_source_txid"],
        source_vout=context["splice_source_vout"],
    )


async def test_splice_in_cli_happy_path_with_confirmation(
    tmp_path: Path,
) -> None:
    context = await _prepare_splice_regtest(tmp_path)
    channel_id = await _open_channel(context)
    await _prepare_splice_utxo(context)

    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        channel_id=channel_id,
        amount=100_000,
        rpc_url=context["rpc_url"],
        confirm=True,
    )
    _assert_cli_success(result)

    assert "Channel splice-in" in result.stdout
    assert "Proceed with channel splice-in?" in result.stdout
    splice_txid = _splice_txid_from_cli(result)
    funding_txid = _assert_splice_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
        channel_id=channel_id,
        splice_txid=splice_txid,
    )
    _assert_joinmarket_input(
        bitcoin_datadir=context["bitcoin_datadir"],
        txid=funding_txid,
        source_txid=context["splice_source_txid"],
        source_vout=context["splice_source_vout"],
    )
