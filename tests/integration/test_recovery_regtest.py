from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.models import Outpoint
from jmlightning.recovery import RecoveryJournal

from .helpers import prepare_regtest

pytestmark = pytest.mark.anyio


def _run_recover(
    *,
    data_dir: Path,
    mnemonic_file: Path,
    cln_socket: str,
    rpc_url: str,
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

    return subprocess.run(
        [
            "jm-lightning",
            "recover",
            "--cln-socket",
            cln_socket,
            "--data-dir",
            str(data_dir),
            "--mnemonic-file",
            str(mnemonic_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


async def _create_persisted_reservation(
    context: dict[str, Any],
    *,
    txid: str | None,
) -> Outpoint:
    adapter = JoinMarketAdapter(context["config"])
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        await wallet.sync_all()
        utxos = wallet.utxo_cache.get(0, [])
        assert utxos
        utxo = utxos[0]
        outpoint = Outpoint(utxo.txid, utxo.vout)
        owner = "recovery-integration-owner"
        assert wallet.reserve_coinjoin_inputs(
            {outpoint.as_tuple()},
            ttl=30 * 60,
            owner=owner,
        )
    finally:
        await adapter.close()

    journal = RecoveryJournal(context["data_dir"])
    record_id = journal.create("open_channel", {"peer_id": context["peer_id"]})
    journal.update(
        record_id,
        locked_outpoints=[outpoint],
        owner_tokens={str(outpoint): owner},
        txid=txid,
    )
    return outpoint


async def test_recover_releases_persisted_reservation_after_restart(
    tmp_path: Path,
) -> None:
    context = await prepare_regtest(tmp_path)
    outpoint = await _create_persisted_reservation(context, txid=None)

    result = _run_recover(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        rpc_url=context["rpc_url"],
    )

    assert result.returncode == 0, (
        f"jm-lightning recover failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "Resolved 1 recovery record(s)." in result.stdout
    assert RecoveryJournal(context["data_dir"]).records() == []

    adapter = JoinMarketAdapter(context["config"])
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        assert outpoint.as_tuple() not in wallet.get_locked_input_outpoints()
    finally:
        await adapter.close()


async def test_recover_keeps_reservation_when_recorded_transaction_exists(
    tmp_path: Path,
) -> None:
    context = await prepare_regtest(tmp_path)
    outpoint = await _create_persisted_reservation(context, txid=None)
    journal = RecoveryJournal(context["data_dir"])
    record_id = journal.records()[0].id
    journal.update(record_id, txid=outpoint.txid)

    result = _run_recover(
        data_dir=context["data_dir"],
        mnemonic_file=context["mnemonic_file"],
        cln_socket=context["cln_socket"],
        rpc_url=context["rpc_url"],
    )

    assert result.returncode == 0, (
        f"jm-lightning recover failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "Resolved 0 recovery record(s)." in result.stdout
    assert len(RecoveryJournal(context["data_dir"]).records()) == 1

    adapter = JoinMarketAdapter(context["config"])
    await adapter.connect()
    try:
        wallet = adapter.require_wallet()
        assert outpoint.as_tuple() in wallet.get_locked_input_outpoints()
    finally:
        await adapter.close()
