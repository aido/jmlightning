from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from jmlightning.operations.multi_open_channel import MultiOpenChannelOperation

from .helpers import assert_channel_normal, lightning_rpc, prepare_regtest

pytestmark = pytest.mark.anyio


PEER_B = "CLN_PEER3_ID"
PEER_B_SOCKET = "CLN_PEER3_RPC_SOCKET"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"regtest integration environment variable {name} is not set")
    return value


def _run_cli(
    *,
    data_dir: Path,
    mnemonic_file: Path,
    cln_socket: str,
    destinations: list[tuple[str, int]],
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
            "BITCOIN__RPC_URL": _required_env("BITCOIN_RPC_URL"),
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
        "multi-open-channel",
        "--cln-socket",
        cln_socket,
        "--data-dir",
        str(data_dir),
        "--mnemonic-file",
        str(mnemonic_file),
    ]
    for peer_id, amount in destinations:
        command.extend(["--destination", f"{peer_id}:{amount}"])
    command.append("--yes")

    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def _assert_cli_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        "jm-lightning multi-open-channel failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _channel_ids(cln_socket: str, peer_id: str) -> set[str]:
    channels = lightning_rpc(cln_socket).listpeerchannels(peer_id).get("channels", [])
    return {
        channel["channel_id"]
        for channel in channels
        if isinstance(channel, dict) and isinstance(channel.get("channel_id"), str)
    }


def _channel_funding_txid(
    cln_socket: str,
    peer_id: str,
    channel_id: str,
) -> str:
    channels = lightning_rpc(cln_socket).listpeerchannels(peer_id).get("channels", [])
    matching = [
        channel
        for channel in channels
        if isinstance(channel, dict)
        and channel.get("channel_id") == channel_id
        and channel.get("funding", {}).get("withheld") is not True
    ]
    assert len(matching) == 1
    txid = matching[0].get("funding_txid")
    assert isinstance(txid, str) and len(txid) == 64
    return txid


async def test_multi_open_channel_happy_path(tmp_path: Path) -> None:
    context = await prepare_regtest(tmp_path)
    peer_b = _required_env(PEER_B)
    peer_b_socket = _required_env(PEER_B_SOCKET)

    previous_channel_ids_a = _channel_ids(context["cln_socket"], context["peer_id"])
    previous_channel_ids_b = _channel_ids(context["cln_socket"], peer_b)

    operation = MultiOpenChannelOperation(
        config=context["config"],
        cln_socket=Path(context["cln_socket"]),
    )
    await operation.execute(
        [
            (context["peer_id"], 100_000),
            (peer_b, 150_000),
        ]
    )

    channel_id_a = assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
        previous_channel_ids=previous_channel_ids_a,
    )
    channel_id_b = assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=peer_b_socket,
        peer_id=peer_b,
        previous_channel_ids=previous_channel_ids_b,
    )

    txid_a = _channel_funding_txid(
        context["cln_socket"], context["peer_id"], channel_id_a
    )
    txid_b = _channel_funding_txid(context["cln_socket"], peer_b, channel_id_b)
    assert txid_a == txid_b


async def test_multi_open_channel_cli_happy_path(tmp_path: Path) -> None:
    context = await prepare_regtest(tmp_path)
    peer_b = _required_env(PEER_B)

    previous_channel_ids_a = _channel_ids(context["cln_socket"], context["peer_id"])
    previous_channel_ids_b = _channel_ids(context["cln_socket"], peer_b)

    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        destinations=[
            (context["peer_id"], 100_000),
            (peer_b, 150_000),
        ],
    )
    _assert_cli_success(result)

    channel_id_a = assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
        previous_channel_ids=previous_channel_ids_a,
    )
    channel_id_b = assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=_required_env(PEER_B_SOCKET),
        peer_id=peer_b,
        previous_channel_ids=previous_channel_ids_b,
    )

    txid_a = _channel_funding_txid(
        context["cln_socket"], context["peer_id"], channel_id_a
    )
    txid_b = _channel_funding_txid(context["cln_socket"], peer_b, channel_id_b)
    assert txid_a == txid_b
