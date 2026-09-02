from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jmwallet.wallet.models import UTXOInfo

from jmlightning.lightning.backend import ChannelFundingStatus, FeePriority
from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.open_channel import (
    FundingCancelledError,
    FundingRecoveryRequiredError,
    OpenChannelOperation,
    confirm_open_channel,
)


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
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.WITHHELD
    cln.get_fee_rate.return_value = 1.0
    cln.cancel_channel_funding.return_value = None

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

    cln.get_channel_funding_status.assert_called_once_with(
        peer_id=peer_id,
        txid="txid",
    )

    cln.cancel_channel_funding.assert_called_once_with(
        peer_id,
    )

    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_psbt_failure_with_absent_state_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.side_effect = RuntimeError("sendpsbt failed")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.ABSENT

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="sendpsbt failed"):
            await operation.execute("02" + "11" * 32)

    cln.get_channel_funding_status.assert_called_once_with(
        peer_id="02" + "11" * 32,
        txid="txid",
    )
    cln.cancel_channel_funding.assert_not_called()
    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


def _build_open_channel_test_doubles() -> tuple[
    Mock, ClassifiedUTXO, Mock, Mock, Mock, Mock
]:
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
    cln.get_fee_rate.return_value = 1.0
    cln.funding_output_type = "p2wsh"

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

    return config, coin, jmadapter, cln, plan, tx_builder


def _patch_open_channel_doubles(
    jmadapter: Mock,
    cln: Mock,
    tx_builder: Mock,
    plan: Mock,
) -> tuple[Any, Any, Any, Any]:
    return (
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
    )


@pytest.mark.anyio
async def test_partial_lock_failure_unlocks_only_successfully_locked_utxos(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()
    second_coin = replace(coin, utxo=replace(coin.utxo, txid="22" * 32, vout=1))

    jmadapter.get_utxos.return_value = [coin, second_coin]
    jmadapter.select_utxos.return_value = [coin.utxo, second_coin.utxo]
    plan.inputs = [coin, second_coin]
    plan.amount = 150_000
    plan.change = 49_900
    jmadapter.lock.side_effect = [None, RuntimeError("lock failed")]

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="lock failed"):
            await operation.execute("02" + "11" * 32)

    assert jmadapter.lock.call_count == 2
    jmadapter.unlock.assert_called_once_with(coin)
    cln.open_channel_start.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_psbt_ambiguous_failure_is_treated_as_broadcast() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.side_effect = RuntimeError("connection lost")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.BROADCAST

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute("02" + "11" * 32)

    cln.cancel_channel_funding.assert_not_called()
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_psbt_failure_with_unknown_state_keeps_utxo_locked() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.side_effect = RuntimeError("connection lost")
    cln.get_channel_funding_status.side_effect = RuntimeError("status unavailable")

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            FundingRecoveryRequiredError,
            match="Unable to determine CLN sendpsbt outcome",
        ):
            await operation.execute("02" + "11" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_psbt_txid_mismatch_keeps_utxo_locked() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.return_value = {"txid": "22" * 32}

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            FundingRecoveryRequiredError,
            match="unexpected funding transaction id",
        ):
            await operation.execute("02" + "11" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_fundchannel_start_unknown_outcome_keeps_utxo_locked() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.open_channel_start.side_effect = RuntimeError("connection lost")

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            FundingRecoveryRequiredError,
            match="fundchannel_start outcome is unknown",
        ):
            await operation.execute("02" + "11" * 32)

    jmadapter.unlock.assert_not_called()
    cln.cancel_channel_funding.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_cancel_failure_keeps_utxo_locked() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.side_effect = RuntimeError("sendpsbt failed")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.WITHHELD
    cln.cancel_channel_funding.side_effect = RuntimeError("cancel failed")

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            FundingRecoveryRequiredError,
            match="Unable to cancel withheld CLN channel funding",
        ):
            await operation.execute("02" + "11" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_fundchannel_complete_failure_with_absent_state_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.open_channel_complete.side_effect = RuntimeError("fundchannel_complete failed")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.ABSENT

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="fundchannel_complete failed"):
            await operation.execute("02" + "11" * 32)

    cln.get_channel_funding_status.assert_called_once_with(
        peer_id="02" + "11" * 32,
        txid="txid",
    )
    cln.cancel_channel_funding.assert_not_called()
    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_local_failure_after_fundchannel_start_cancels_and_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    jmadapter.get_change_address.side_effect = RuntimeError("change address failed")

    patches = _patch_open_channel_doubles(jmadapter, cln, tx_builder, plan)

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="change address failed"):
            await operation.execute("02" + "11" * 32)

    cln.cancel_channel_funding.assert_called_once_with("02" + "11" * 32)
    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_tx_build_failure_after_fundchannel_start_cancels_and_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    tx_builder.build_and_sign_funding_tx.side_effect = RuntimeError(
        "transaction build failed"
    )

    patches = _patch_open_channel_doubles(jmadapter, cln, tx_builder, plan)

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="transaction build failed"):
            await operation.execute("02" + "11" * 32)

    cln.cancel_channel_funding.assert_called_once_with("02" + "11" * 32)
    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_failure_after_fundchannel_start_cancel_failure_keeps_utxo_locked() -> (
    None
):
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    tx_builder.build_and_sign_funding_tx.side_effect = RuntimeError(
        "transaction build failed"
    )
    cln.cancel_channel_funding.side_effect = RuntimeError("cancel failed")

    patches = _patch_open_channel_doubles(jmadapter, cln, tx_builder, plan)

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            FundingRecoveryRequiredError,
            match="local transaction preparation failed",
        ):
            await operation.execute("02" + "11" * 32)

    cln.cancel_channel_funding.assert_called_once_with("02" + "11" * 32)
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_successful_send_psbt_keeps_inputs_frozen() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.return_value = {"txid": "txid"}

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute("02" + "11" * 32)

    jmadapter.unlock.assert_not_called()
    cln.cancel_channel_funding.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_successful_broadcast_does_not_unlock_inputs() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.return_value = {"txid": "txid"}

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute("02" + "11" * 32)

    cln.cancel_channel_funding.assert_not_called()
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_successful_broadcast_survives_close_failure() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    cln.send_psbt.return_value = {"txid": "txid"}
    jmadapter.close.side_effect = RuntimeError("close failed")

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute("02" + "11" * 32)

    cln.cancel_channel_funding.assert_not_called()
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_confirmation_happens_before_cln_completion() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    events: list[str] = []

    def confirm(
        peer_id: str,
        plan: object,
        tx: object,
        txid: str,
        funding_address: str,
    ) -> bool:
        events.append("confirm")
        return True

    def complete(**kwargs: object) -> dict[str, object]:
        events.append("complete")
        return {}

    cln.open_channel_complete.side_effect = complete
    cln.send_psbt.return_value = {"txid": "txid"}

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute(
            "02" + "11" * 32,
            confirm=confirm,
        )

    assert events == ["confirm", "complete"]
    cln.send_psbt.assert_called_once()


@pytest.mark.anyio
async def test_confirmation_rejection_prevents_funding() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    def confirm(
        peer_id: str,
        plan: object,
        tx: object,
        txid: str,
        funding_address: str,
    ) -> bool:
        return False

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            FundingCancelledError,
            match="Channel funding cancelled by user",
        ):
            await operation.execute(
                "02" + "11" * 32,
                confirm=confirm,
            )

    cln.open_channel_complete.assert_not_called()
    cln.send_psbt.assert_not_called()
    cln.cancel_channel_funding.assert_called_once_with(
        "02" + "11" * 32,
    )
    jmadapter.unlock.assert_called_once_with(coin)


@pytest.mark.anyio
async def test_confirmation_receives_actual_transaction() -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_open_channel_test_doubles()

    captured: dict[str, object] = {}

    def confirm(
        peer_id: str,
        received_plan: object,
        tx: object,
        txid: str,
        funding_address: str,
    ) -> bool:
        captured["peer_id"] = peer_id
        captured["plan"] = received_plan
        captured["tx"] = tx
        captured["txid"] = txid
        captured["funding_address"] = funding_address
        return True

    cln.send_psbt.return_value = {"txid": "txid"}

    patches = _patch_open_channel_doubles(
        jmadapter,
        cln,
        tx_builder,
        plan,
    )

    with patches[0], patches[1], patches[2], patches[3]:
        operation = OpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute(
            "02" + "11" * 32,
            confirm=confirm,
        )

    assert captured["peer_id"] == "02" + "11" * 32
    assert captured["plan"] is plan
    assert captured["tx"] is tx_builder.build_and_sign_funding_tx.return_value[0]
    assert captured["txid"] == "txid"
    assert captured["funding_address"] == "bc1qfunding"


def test_confirm_open_channel_displays_transaction_and_accepts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _config, coin, _jmadapter, _cln, plan, _tx_builder = (
        _build_open_channel_test_doubles()
    )
    plan.inputs = [coin]
    plan.amount = 100_000
    plan.fee = 100
    plan.vsize = 140
    plan.change = 99_900
    plan.warnings = ["high fee rate"]

    tx = Mock()
    tx.inputs = [Mock()]

    monkeypatch.setattr(
        "jmlightning.operations.open_channel.typer.confirm",
        lambda message, default=False: True,
    )

    assert (
        confirm_open_channel(
            peer_id="02" + "11" * 32,
            plan=plan,
            tx=tx,
            txid="aa" * 32,
            funding_address="bc1qfunding",
        )
        is True
    )

    output = capsys.readouterr().out
    assert "Channel funding transaction" in output
    assert "Peer:" in output
    assert "Funding address:" in output
    assert "Funding amount:   100,000 sats" in output
    assert "Fee:              100 sats" in output
    assert "Virtual size:     140 vbytes" in output
    assert "Transaction ID:" in output
    assert "Inputs:" in output
    assert f"{coin.utxo.txid}:{coin.utxo.vout}" in output
    assert "Outputs:" in output
    assert "Channel funding: 100,000 sats" in output
    assert "JoinMarket change: 99,900 sats" in output
    assert "Warnings:" in output
    assert "WARNING: high fee rate" in output
