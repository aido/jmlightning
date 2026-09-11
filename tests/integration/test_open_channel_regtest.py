from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from jmlightning.operations.open_channel import OpenChannelOperation

from .helpers import assert_channel_normal, prepare_regtest

pytestmark = pytest.mark.anyio


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


async def test_open_channel_happy_path(tmp_path: Path) -> None:
    context = await prepare_regtest(tmp_path)

    operation = OpenChannelOperation(
        config=context["config"],
        cln_socket=Path(context["cln_socket"]),
    )
    await operation.execute(context["peer_id"])

    assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
    )


async def test_open_channel_cli_happy_path(tmp_path: Path) -> None:
    context = await prepare_regtest(tmp_path)

    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        peer_id=context["peer_id"],
        rpc_url=context["rpc_url"],
    )
    _assert_cli_success(result)

    assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
    )


async def test_open_channel_cli_happy_path_with_confirmation(
    tmp_path: Path,
) -> None:
    context = await prepare_regtest(tmp_path)

    result = _run_cli(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        peer_id=context["peer_id"],
        rpc_url=context["rpc_url"],
        confirm=True,
    )
    _assert_cli_success(result)

    assert_channel_normal(
        bitcoin_datadir=context["bitcoin_datadir"],
        cln_socket=context["cln_socket"],
        peer_socket=context["peer_socket"],
        peer_id=context["peer_id"],
    )
