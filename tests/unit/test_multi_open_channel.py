import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jmwallet.wallet.models import UTXOInfo

from jmlightning.lightning.backend import ChannelFundingStatus, FeePriority
from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.multi_open_channel import (
    MultiOpenChannelCancelledError,
    MultiOpenChannelOperation,
    MultiOpenChannelRecoveryRequiredError,
)
from jmlightning.planner import ExecutionPlan, Planner

PEER_A = "02" + "11" * 32
PEER_B = "02" + "22" * 32


@pytest.mark.anyio
async def test_execute_rejects_empty_destinations() -> None:
    operation = MultiOpenChannelOperation(Mock(), Path("/tmp/lightning-rpc"))

    with pytest.raises(ValueError, match="At least one channel destination"):
        await operation.execute([])


@pytest.mark.anyio
async def test_execute_rejects_duplicate_destinations() -> None:
    operation = MultiOpenChannelOperation(Mock(), Path("/tmp/lightning-rpc"))

    with pytest.raises(ValueError, match="destinations must be unique"):
        await operation.execute([(PEER_A, 100_000), (PEER_A, 200_000)])


@pytest.mark.anyio
async def test_execute_rejects_non_positive_amount() -> None:
    operation = MultiOpenChannelOperation(Mock(), Path("/tmp/lightning-rpc"))

    with pytest.raises(ValueError, match="amounts must be positive"):
        await operation.execute([(PEER_A, 0)])


@pytest.mark.anyio
async def test_execute_funds_multiple_channels_with_one_transaction() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    destinations = [(PEER_A, 100_000), (PEER_B, 150_000)]

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute(destinations)

    jmadapter.connect.assert_awaited_once()
    jmadapter.select_utxos.assert_called_once()
    jmadapter.lock.assert_called_once_with(coin)
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()

    assert cln.open_channel_start.call_count == 2
    cln.open_channel_start.assert_any_call(
        peer_id=PEER_A,
        amount=100_000,
        announce=config.announce,
    )
    cln.open_channel_start.assert_any_call(
        peer_id=PEER_B,
        amount=150_000,
        announce=config.announce,
    )

    tx_builder.build_and_sign_multifunding_tx.assert_called_once_with(
        plan=plan,
        funding_addresses=["bc1qfunding-a", "bc1qfunding-b"],
        change_address="bc1qchange",
        wallet=jmadapter.require_wallet.return_value,
    )
    assert cln.open_channel_complete.call_count == 2
    cln.open_channel_complete.assert_any_call(peer_id=PEER_A, psbt=b"signed-psbt")
    cln.open_channel_complete.assert_any_call(peer_id=PEER_B, psbt=b"signed-psbt")
    cln.send_psbt.assert_called_once_with(b"signed-psbt")
    cln.cancel_channel_funding.assert_not_called()


@pytest.mark.anyio
async def test_start_failure_is_ambiguous_and_keeps_utxos_locked() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.open_channel_start.side_effect = ["bc1qfunding-a", RuntimeError("start failed")]

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="channel start failed.*ambiguous",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    cln.cancel_channel_funding.assert_called_once_with(PEER_A)
    tx_builder.build_and_sign_multifunding_tx.assert_not_called()
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_user_decline_cancels_all_channels_and_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelCancelledError,
            match="cancelled by user",
        ):
            await operation.execute(
                [(PEER_A, 100_000), (PEER_B, 150_000)],
                confirm=lambda *_: False,
            )

    assert cln.cancel_channel_funding.call_count == 2
    jmadapter.unlock.assert_called_once_with(coin)
    cln.send_psbt.assert_not_called()


@pytest.mark.anyio
async def test_user_decline_with_cancel_failure_requires_recovery() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.cancel_channel_funding.side_effect = RuntimeError("cancel failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="Unable to cancel all CLN channel funding",
        ):
            await operation.execute(
                [(PEER_A, 100_000), (PEER_B, 150_000)],
                confirm=lambda *_: False,
            )

    jmadapter.unlock.assert_not_called()


@pytest.mark.anyio
async def test_transaction_preparation_failure_cancels_all_channels() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    tx_builder.build_and_sign_multifunding_tx.side_effect = RuntimeError("build failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="Shared funding transaction preparation failed",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    assert cln.cancel_channel_funding.call_count == 2
    cln.cancel_channel_funding.assert_any_call(PEER_A)
    cln.cancel_channel_funding.assert_any_call(PEER_B)
    cln.open_channel_complete.assert_not_called()
    cln.send_psbt.assert_not_called()
    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_failure_with_broadcast_state_keeps_utxos_locked() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.send_psbt.side_effect = RuntimeError("connection lost")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.BROADCAST

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="Unable to determine CLN broadcast outcome",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    assert cln.get_channel_funding_status.call_count == 2
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_start_failure_with_cancel_failure_requires_recovery_keeps_locked() -> (
    None
):
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.open_channel_start.side_effect = [
        "bc1qfunding-a",
        RuntimeError("start failed"),
    ]
    cln.cancel_channel_funding.side_effect = RuntimeError("cancel failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="A channel start failed",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    jmadapter.unlock.assert_not_called()
    cln.cancel_channel_funding.assert_called_once_with(PEER_A)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_completion_failure_cancels_started_channels_and_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.open_channel_complete.side_effect = [None, RuntimeError("complete failed")]
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.WITHHELD

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="complete failed"):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    assert cln.get_channel_funding_status.call_count == 2
    assert cln.cancel_channel_funding.call_count == 2
    jmadapter.unlock.assert_called_once_with(coin)
    cln.send_psbt.assert_not_called()


@pytest.mark.anyio
async def test_completion_failure_with_cancel_failure_requires_recovery() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.open_channel_complete.side_effect = [None, RuntimeError("complete failed")]
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.WITHHELD
    cln.cancel_channel_funding.side_effect = RuntimeError("cancel failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="Unable to cancel CLN channel funding",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    jmadapter.unlock.assert_not_called()
    assert cln.cancel_channel_funding.call_count == 2


@pytest.mark.anyio
async def test_completion_failure_with_status_error_requires_recovery() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.open_channel_complete.side_effect = [None, RuntimeError("complete failed")]
    cln.get_channel_funding_status.side_effect = RuntimeError("status failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="Multi-channel completion outcome is ambiguous",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    jmadapter.unlock.assert_not_called()
    cln.cancel_channel_funding.assert_not_called()


@pytest.mark.anyio
async def test_send_failure_with_withheld_state_cancels_and_unlocks() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.send_psbt.side_effect = RuntimeError("connection lost")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.WITHHELD

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="connection lost"):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    assert cln.get_channel_funding_status.call_count == 2
    assert cln.cancel_channel_funding.call_count == 2
    jmadapter.unlock.assert_called_once_with(coin)


@pytest.mark.anyio
async def test_operation_failure_with_cleanup_failure_requires_recovery() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.send_psbt.side_effect = RuntimeError("send failed")
    jmadapter.unlock.side_effect = RuntimeError("unlock failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="failed and cleanup also failed",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    jmadapter.unlock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_failure_with_cancel_failure_requires_recovery() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    cln.send_psbt.side_effect = RuntimeError("connection lost")
    cln.get_channel_funding_status.return_value = ChannelFundingStatus.WITHHELD
    cln.cancel_channel_funding.side_effect = RuntimeError("cancel failed")

    with _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            MultiOpenChannelRecoveryRequiredError,
            match="Unable to cancel withheld CLN channel funding",
        ):
            await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    jmadapter.unlock.assert_not_called()


@pytest.mark.anyio
async def test_planner_reselection_retries_after_insufficient_funds() -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_test_doubles()
    insufficient = ValueError("Insufficient funds after fees.")

    with (
        _patch_multi_open_channel_doubles(jmadapter, cln, tx_builder, plan),
        patch(
            "jmlightning.operations.multi_open_channel.Planner.build_multi_plan",
            side_effect=[insufficient, plan],
        ) as build_multi_plan,
    ):
        operation = MultiOpenChannelOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute([(PEER_A, 100_000), (PEER_B, 150_000)])

    assert jmadapter.select_utxos.call_count == 2
    assert build_multi_plan.call_count == 2
    assert (
        jmadapter.select_utxos.call_args_list[1].kwargs["target_amount"]
        > jmadapter.select_utxos.call_args_list[0].kwargs["target_amount"]
    )


def _build_test_doubles() -> tuple[
    Mock, ClassifiedUTXO, Mock, Mock, ExecutionPlan, Mock
]:
    config = Mock()
    config.data_dir = Path(tempfile.mkdtemp(prefix="jmlightning-recovery-test-"))
    config.mixdepth = 0
    config.announce = False
    config.fee_priority = FeePriority.NORMAL

    utxo = UTXOInfo(
        txid="11" * 32,
        vout=0,
        value=500_000,
        mixdepth=0,
        address="bc1qtest",
        confirmations=6,
        scriptpubkey="0014" + "00" * 20,
        path="m/84'/0'/0'/0/0",
    )
    coin = ClassifiedUTXO(utxo=utxo, status="cj-out")

    jmadapter = Mock()
    jmadapter.connect = AsyncMock()
    jmadapter.close = AsyncMock()
    jmadapter.get_utxos.return_value = [coin]
    jmadapter.select_utxos.return_value = [utxo]
    jmadapter.get_change_address.return_value = "bc1qchange"

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0
    cln.funding_output_type = "p2wsh"
    cln.open_channel_start.side_effect = ["bc1qfunding-a", "bc1qfunding-b"]
    cln.send_psbt.return_value = {"txid": "txid"}

    plan = Planner().build_multi_plan(
        selected_coins=[coin],
        target_amounts=[100_000, 150_000],
        fee_rate=1.0,
        funding_output_types=["p2wsh", "p2wsh"],
    )

    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "txid",
        b"signed-psbt",
    )

    return config, coin, jmadapter, cln, plan, tx_builder


@contextmanager
def _patch_multi_open_channel_doubles(
    jmadapter: Mock,
    cln: Mock,
    tx_builder: Mock,
    plan: ExecutionPlan,
) -> Iterator[None]:
    with (
        patch(
            "jmlightning.operations.multi_open_channel.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch(
            "jmlightning.operations.multi_open_channel.CLNBackend",
            return_value=cln,
        ),
        patch(
            "jmlightning.operations.multi_open_channel.TxBuilder",
            return_value=tx_builder,
        ),
        patch(
            "jmlightning.operations.multi_open_channel.Planner.build_multi_plan",
            return_value=plan,
        ),
    ):
        yield
