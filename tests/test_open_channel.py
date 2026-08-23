from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jmwallet.wallet.models import UTXOInfo

from jmlightning.lightning.backend import FeePriority
from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.open_channel import OpenChannelOperation


@pytest.mark.anyio
async def test_send_psbt_failure_cancels_withheld_channel() -> None:
    peer_id = "02" + "11" * 32

    config = Mock()
    config.amount = 100_000
    config.mixdepth = 0
    config.announce = False
    config.fee_priority = FeePriority.NORMAL

    utxo = UTXOInfo(
        txid="11" * 32,
        vout=0,
        value=200_000,
        mixdepth=0,
        address="bc1qtest",
        confirmations=6,
        scriptpubkey="0014" + "00" * 20,
        path="m/84'/0'/0'/0/0",
    )

    coin = ClassifiedUTXO(
        utxo=utxo,
        status="cj-out",
    )

    jmadapter = Mock()
    jmadapter.connect = AsyncMock()
    jmadapter.close = AsyncMock()
    jmadapter.get_utxos.return_value = [coin]
    jmadapter.select_utxos.return_value = [utxo]

    cln = Mock()
    cln.open_channel_start.return_value = "bc1qfunding"
    cln.open_channel_complete.return_value = {}
    cln.send_psbt.side_effect = RuntimeError("sendpsbt failed")
    cln.get_fee_rate.return_value = 1.0
    cln.rpc.fundchannel_cancel.return_value = {}

    plan = Mock()
    plan.inputs = [coin]
    plan.amount = 100_000
    plan.fee = 100
    plan.change = 99_900
    plan.warnings = []

    tx_builder = Mock()
    tx_builder.build_and_sign_funding_tx.return_value = (
        Mock(),
        "txid",
        0,
        b"signed-psbt",
    )

    with (
        patch(
            "jmlightning.operations.open_channel.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch(
            "jmlightning.operations.open_channel.CLNBackend",
            return_value=cln,
        ),
        patch(
            "jmlightning.operations.open_channel.TxBuilder",
            return_value=tx_builder,
        ),
        patch(
            "jmlightning.operations.open_channel.Planner.build_plan",
            return_value=plan,
        ),
    ):
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="sendpsbt failed"):
            await operation.execute(peer_id)

    cln.open_channel_start.assert_called_once_with(
        peer_id=peer_id,
        amount=plan.amount,
        announce=config.announce,
    )

    cln.open_channel_complete.assert_called_once_with(
        peer_id=peer_id,
        psbt=b"signed-psbt",
    )

    cln.send_psbt.assert_called_once_with(
        b"signed-psbt",
    )

    cln.rpc.fundchannel_cancel.assert_called_once_with(
        node_id=peer_id,
    )

    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()
