from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from jmwallet.wallet.models import UTXOInfo

from jmlightning.lightning.backend import FeePriority
from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.peerswap import (
    PeerSwapOutput,
    PeerSwapPhase,
    PeerSwapPrepareTxOperation,
    PeerSwapPrepareTxRequest,
    PreparedPeerSwapTransaction,
)

PEERSWAP_OUTPUT_ADDRESS = "bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2"
PEERSWAP_CHANGE_ADDRESS = "bcrt1qxvenxvenxvenxvenxvenxvenxvenxvenztev8a"
PEERSWAP_INPUT_ADDRESS = "bcrt1qzyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3lgth6c"


def _request() -> PeerSwapPrepareTxRequest:
    return PeerSwapPrepareTxRequest.from_rpc(
        {
            "outputs": [{PEERSWAP_OUTPUT_ADDRESS: 100_000}],
            "feerate": "urgent",
        }
    )


def _coin() -> ClassifiedUTXO:
    return ClassifiedUTXO(
        utxo=UTXOInfo(
            txid="11" * 32,
            vout=0,
            value=150_000,
            mixdepth=0,
            address=PEERSWAP_INPUT_ADDRESS,
            confirmations=6,
            scriptpubkey="0014" + "11" * 20,
            path="m/84'/1'/0'/0/0",
        ),
        status="cj-out",
    )


@pytest.mark.anyio
async def test_txprepare_builds_and_locks_joinmarket_transaction() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0

    plan = Mock()
    plan.funding_outputs = [Mock()]

    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "22" * 32,
        b"signed-psbt",
    )

    config = Mock()
    config.mixdepth = 0
    config.fee_priority = FeePriority.NORMAL

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter",
            return_value=adapter,
        ),
        patch(
            "jmlightning.operations.peerswap.CLNBackend",
            return_value=cln,
        ),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch(
            "jmlightning.operations.peerswap.TxBuilder",
            return_value=tx_builder,
        ),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        prepared = await operation.execute(request)

    assert prepared.txid == "22" * 32
    assert prepared.locked == [coin]
    assert prepared.phase is PeerSwapPhase.PREPARED
    cln.get_fee_rate.assert_called_once_with(feerate="urgent")
    adapter.lock.assert_called_once_with(coin)
    tx_builder.build_and_sign_multifunding_tx.assert_called_once_with(
        plan=plan,
        funding_addresses=[PEERSWAP_OUTPUT_ADDRESS],
        change_address=PEERSWAP_CHANGE_ADDRESS,
        wallet=adapter.require_wallet.return_value,
        finalise_psbt=True,
    )
    adapter.close.assert_not_awaited()


@pytest.mark.anyio
async def test_txprepare_raises_fee_to_bitcoin_mempool_minimum() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=0.1)
    adapter.require_wallet.return_value = Mock()

    cln = Mock()
    cln.get_fee_rate.return_value = 0.06325

    plan = Mock()
    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "22" * 32,
        b"signed-psbt",
    )

    config = Mock(mixdepth=0)

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter",
            return_value=adapter,
        ),
        patch(
            "jmlightning.operations.peerswap.CLNBackend",
            return_value=cln,
        ),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ) as build_multi_plan,
        patch(
            "jmlightning.operations.peerswap.TxBuilder",
            return_value=tx_builder,
        ),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        await operation.execute(request)

    adapter.get_mempool_min_fee.assert_awaited_once()
    cln.get_fee_rate.assert_called_once_with(feerate="urgent")
    assert build_multi_plan.call_args.kwargs["fee_rate"] == 0.1
    assert tx_builder.build_and_sign_multifunding_tx.call_count == 1


@pytest.mark.anyio
async def test_txprepare_releases_locks_when_transaction_build_fails() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0

    plan = Mock()
    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.side_effect = RuntimeError("build failed")

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter", return_value=adapter
        ),
        patch("jmlightning.operations.peerswap.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch("jmlightning.operations.peerswap.TxBuilder", return_value=tx_builder),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=Mock(mixdepth=0),
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="build failed"):
            await operation.execute(request)

    adapter.lock.assert_called_once_with(coin)
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_prepared_result_matches_cln_txprepare_response() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = "bcrt1qchange"
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0

    plan = Mock()
    plan.funding_outputs = [Mock()]

    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "22" * 32,
        b"prepared-psbt",
    )

    config = Mock(mixdepth=0)

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter", return_value=adapter
        ),
        patch("jmlightning.operations.peerswap.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch("jmlightning.operations.peerswap.TxBuilder", return_value=tx_builder),
        patch(
            "jmlightning.operations.peerswap.serialize_transaction",
            return_value=b"unsigned-tx",
        ),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        prepared = await operation.execute(request)
        result = await operation.prepared_result(prepared.txid)

    assert result == {
        "unsigned_tx": "756e7369676e65642d7478",
        "txid": "22" * 32,
        "psbt": "cHJlcGFyZWQtcHNidA==",
    }


def test_prepared_transaction_retains_explicit_reservation_state() -> None:
    coin = _coin()
    adapter = Mock()

    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"",
        reservations=(coin,),
    )

    prepared.renew_reservations()

    assert prepared.reservations == (coin,)
    adapter.renew_locks.assert_called_once_with([coin])


@pytest.mark.anyio
async def test_prepared_transaction_legacy_construction_derives_reservations() -> None:
    coin = _coin()
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=Mock(),
        psbt=b"",
    )

    assert prepared.reservations == (coin,)


@pytest.mark.anyio
async def test_send_broadcasts_prepared_transaction_and_releases_locks() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()
    adapter.broadcast = AsyncMock(return_value="22" * 32)

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0

    plan = Mock()
    plan.funding_outputs = [Mock()]

    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "22" * 32,
        b"signed-psbt",
    )

    config = Mock(mixdepth=0)

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter", return_value=adapter
        ),
        patch("jmlightning.operations.peerswap.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch("jmlightning.operations.peerswap.TxBuilder", return_value=tx_builder),
        patch(
            "jmlightning.operations.peerswap.serialize_transaction",
            return_value=b"signed-tx",
        ),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        prepared = await operation.execute(request)
        result = await operation.send(prepared.txid)

    assert result["tx"] == "7369676e65642d7478"
    assert result["txid"] == "22" * 32
    assert result["psbt"] == "c2lnbmVkLXBzYnQ="
    adapter.broadcast.assert_awaited_once_with(prepared.tx)
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_awaited_once()
    assert operation._prepared == {}
    assert prepared.phase is PeerSwapPhase.BROADCAST


@pytest.mark.anyio
async def test_send_keeps_prepared_transaction_when_broadcast_fails() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()
    adapter.broadcast = AsyncMock(side_effect=RuntimeError("broadcast failed"))

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0
    plan = Mock()
    plan.funding_outputs = [Mock()]
    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "22" * 32,
        b"signed-psbt",
    )

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter", return_value=adapter
        ),
        patch("jmlightning.operations.peerswap.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch("jmlightning.operations.peerswap.TxBuilder", return_value=tx_builder),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=Mock(mixdepth=0),
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        prepared = await operation.execute(request)

        with pytest.raises(RuntimeError, match="broadcast failed"):
            await operation.send(prepared.txid)

    adapter.unlock.assert_not_called()
    adapter.close.assert_not_awaited()
    assert operation._prepared[prepared.txid] is prepared


@pytest.mark.anyio
async def test_send_rejects_non_prepared_phase() -> None:
    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    adapter = Mock()
    adapter.broadcast = AsyncMock()
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[],
        adapter=adapter,
        psbt=b"",
        phase=PeerSwapPhase.DISCARDED,
    )
    operation._prepared[prepared.txid] = prepared

    with pytest.raises(ValueError, match="is in discarded state"):
        await operation.send(prepared.txid)

    adapter.broadcast.assert_not_awaited()
    assert operation._prepared[prepared.txid] is prepared


@pytest.mark.anyio
async def test_discard_retries_cleanup_for_broadcast_transaction() -> None:
    coin = _coin()
    adapter = Mock()
    adapter.unlock.side_effect = RuntimeError("unlock failed")
    adapter.close = AsyncMock()

    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(version=2, inputs=[], outputs=[], locktime=0),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"",
        phase=PeerSwapPhase.BROADCAST,
    )
    operation._prepared[prepared.txid] = prepared

    with pytest.raises(RuntimeError, match="Failed to fully clean up"):
        await operation.discard(prepared.txid)
    assert operation._prepared[prepared.txid] is prepared
    assert prepared.phase is PeerSwapPhase.BROADCAST
    adapter.close.assert_not_awaited()

    adapter.unlock.side_effect = None
    await operation.discard(prepared.txid)

    assert operation._prepared == {}
    assert prepared.phase is PeerSwapPhase.BROADCAST
    adapter.unlock.assert_has_calls([call(coin), call(coin)])
    assert adapter.unlock.call_count == 2
    adapter.close.assert_awaited_once()


@pytest.mark.parametrize("feerate", [True, False, 1.5, []])
def test_txprepare_rejects_invalid_feerate_types(feerate: object) -> None:
    with pytest.raises(ValueError, match="txprepare feerate"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{PEERSWAP_OUTPUT_ADDRESS: 100_000}],
                "feerate": feerate,
            }
        )


@pytest.mark.parametrize("minconf", [True, False, 1.5, "1"])
def test_txprepare_rejects_invalid_minconf_types(minconf: object) -> None:
    with pytest.raises(ValueError, match="txprepare minconf must be a uint32"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{PEERSWAP_OUTPUT_ADDRESS: 100_000}],
                "minconf": minconf,
            }
        )


def test_txprepare_rejects_invalid_sat_amount() -> None:
    with pytest.raises(ValueError, match="Invalid txprepare output amount"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{PEERSWAP_OUTPUT_ADDRESS: "not-a-number-sat"}],
            }
        )


@pytest.mark.anyio
async def test_send_rejects_unexpected_broadcast_txid() -> None:
    coin = _coin()
    adapter = Mock()
    adapter.broadcast = AsyncMock(return_value="33" * 32)
    adapter.close = AsyncMock()

    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"",
        phase=PeerSwapPhase.PREPARED,
    )
    operation._prepared[prepared.txid] = prepared

    with pytest.raises(RuntimeError, match="unexpected transaction id"):
        await operation.send(prepared.txid)

    adapter.unlock.assert_not_called()
    adapter.close.assert_not_awaited()
    assert operation._prepared[prepared.txid] is prepared


@pytest.mark.anyio
async def test_send_completes_broadcast_even_when_cleanup_fails() -> None:
    coin = _coin()
    adapter = Mock()
    adapter.broadcast = AsyncMock(return_value="22" * 32)
    adapter.unlock.side_effect = RuntimeError("unlock failed")
    adapter.close = AsyncMock(side_effect=RuntimeError("close failed"))

    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"",
        phase=PeerSwapPhase.PREPARED,
    )
    operation._prepared[prepared.txid] = prepared

    with patch(
        "jmlightning.operations.peerswap.serialize_transaction",
        return_value=b"signed-tx",
    ):
        result = await operation.send(prepared.txid)

    assert result["txid"] == "22" * 32
    assert prepared.phase is PeerSwapPhase.BROADCAST
    assert operation._prepared[prepared.txid] is prepared
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_not_awaited()

    adapter.unlock.side_effect = None
    adapter.close.side_effect = None
    await operation.close()
    assert operation._prepared == {}
    assert adapter.unlock.call_count == 2
    assert adapter.close.await_count == 1


def test_txprepare_rejects_invalid_output_address() -> None:
    with pytest.raises(ValueError, match="Invalid txprepare output address 0"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{"not-a-bitcoin-address": 100_000}],
            }
        )


def test_txprepare_accepts_glightning_sat_output_amount() -> None:
    request = PeerSwapPrepareTxRequest.from_rpc(
        {
            "outputs": [{PEERSWAP_OUTPUT_ADDRESS: "100000sat"}],
        }
    )

    assert request.outputs[0].amount == 100_000


def test_txprepare_accepts_valid_regtest_output_address() -> None:
    request = PeerSwapPrepareTxRequest.from_rpc(
        {
            "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
        }
    )

    assert request.outputs[0] == PeerSwapOutput(
        address="bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2",
        amount=100_000,
    )


def test_txprepare_rejects_invalid_output_before_accepting_later_outputs() -> None:
    with pytest.raises(ValueError, match="Invalid txprepare output address 0"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [
                    {"not-a-bitcoin-address": 100_000},
                    {"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 200_000},
                ],
            }
        )


@pytest.mark.anyio
async def test_discard_returns_cln_txdiscard_response() -> None:
    coin = _coin()
    adapter = Mock()
    adapter.close = AsyncMock()

    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"",
        phase=PeerSwapPhase.PREPARED,
    )
    operation._prepared["22" * 32] = prepared

    with patch(
        "jmlightning.operations.peerswap.PeerSwapPrepareTxOperation._unsigned_tx",
        return_value="02000000",
    ):
        result = await operation.discard("22" * 32)

    assert result == {"unsigned_tx": "02000000", "txid": "22" * 32}
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_awaited_once()
    assert operation._prepared == {}
    assert prepared.phase is PeerSwapPhase.DISCARDED


@pytest.mark.anyio
async def test_discard_retains_state_when_unlock_fails() -> None:
    coin = _coin()
    adapter = Mock()
    adapter.close = AsyncMock()
    adapter.unlock.side_effect = [RuntimeError("unlock failed"), None]

    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"",
        phase=PeerSwapPhase.PREPARED,
    )
    operation._prepared[prepared.txid] = prepared

    with pytest.raises(RuntimeError, match="Failed to fully clean up"):
        await operation.discard("22" * 32)

    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_not_awaited()
    assert operation._prepared[prepared.txid] is prepared
    assert prepared.phase is PeerSwapPhase.PREPARED

    with patch(
        "jmlightning.operations.peerswap.PeerSwapPrepareTxOperation._unsigned_tx",
        return_value="02000000",
    ):
        result = await operation.discard(prepared.txid)

    assert result == {"unsigned_tx": "02000000", "txid": prepared.txid}
    assert operation._prepared == {}
    assert prepared.phase.value == PeerSwapPhase.DISCARDED.value
    assert adapter.unlock.call_count == 2
    adapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_close_discards_all_prepared_transactions() -> None:
    first_coin = _coin()
    second_coin = ClassifiedUTXO(
        utxo=UTXOInfo(
            txid="33" * 32,
            vout=1,
            value=175_000,
            mixdepth=0,
            address=PEERSWAP_INPUT_ADDRESS,
            confirmations=6,
            scriptpubkey="0014" + "11" * 20,
            path="m/84'/1'/0'/0/1",
        ),
        status="cj-out",
    )

    first_adapter = Mock()
    first_adapter.close = AsyncMock()
    second_adapter = Mock()
    second_adapter.close = AsyncMock()

    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    operation._prepared = {
        "22" * 32: PreparedPeerSwapTransaction(
            tx=Mock(),
            txid="22" * 32,
            locked=[first_coin],
            adapter=first_adapter,
            psbt=b"",
            phase=PeerSwapPhase.PREPARED,
        ),
        "33" * 32: PreparedPeerSwapTransaction(
            tx=Mock(),
            txid="33" * 32,
            locked=[second_coin],
            adapter=second_adapter,
            psbt=b"",
            phase=PeerSwapPhase.PREPARED,
        ),
    }

    await operation.close()

    first_adapter.unlock.assert_called_once_with(first_coin)
    first_adapter.close.assert_awaited_once()
    second_adapter.unlock.assert_called_once_with(second_coin)
    second_adapter.close.assert_awaited_once()
    assert operation._prepared == {}


def test_txprepare_rejects_malformed_explicit_utxo() -> None:
    with pytest.raises(ValueError, match="Invalid txprepare UTXO outpoint"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
                "utxos": ["not-an-outpoint"],
            }
        )


def test_txprepare_rejects_duplicate_explicit_utxo() -> None:
    outpoint = "11" * 32 + ":0"

    with pytest.raises(ValueError, match="Duplicate txprepare UTXO outpoint"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
                "utxos": [outpoint, outpoint],
            }
        )


def test_txprepare_rejects_outpoint_vout_over_uint32() -> None:
    with pytest.raises(ValueError, match="Invalid txprepare UTXO outpoint"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
                "utxos": ["11" * 32 + ":4294967296"],
            }
        )


def test_txprepare_rejects_negative_minconf() -> None:
    with pytest.raises(ValueError, match="txprepare minconf must be a uint32"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
                "minconf": -1,
            }
        )


def test_txprepare_rejects_minconf_over_uint32() -> None:
    with pytest.raises(ValueError, match="txprepare minconf must be a uint32"):
        PeerSwapPrepareTxRequest.from_rpc(
            {
                "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
                "minconf": 0x1_0000_0000,
            }
        )


def test_txprepare_accepts_max_uint32_minconf() -> None:
    request = PeerSwapPrepareTxRequest.from_rpc(
        {
            "outputs": [{"bcrt1qqqgjyv6y24n80zye42aueh0wluqpzg3n9tg8m2": 100_000}],
            "minconf": 0xFFFFFFFF,
        }
    )

    assert request.minconf == 0xFFFFFFFF


def test_txprepare_uses_explicit_utxos() -> None:
    request = PeerSwapPrepareTxRequest.from_rpc(
        {
            "outputs": [{PEERSWAP_OUTPUT_ADDRESS: 100_000}],
            "utxos": ["11" * 32 + ":0"],
        }
    )
    coin = _coin()
    adapter = Mock()
    planner = Mock()
    operation = PeerSwapPrepareTxOperation(
        config=Mock(mixdepth=0),
        cln_socket=Path("/tmp/lightning-rpc"),
    )

    selected = operation._select_utxos(
        request=request,
        allowed=[coin],
        adapter=adapter,
        fee_rate=1.0,
        planner=planner,
    )

    assert selected == [coin]
    adapter.select_utxos.assert_not_called()


@pytest.mark.anyio
async def test_txprepare_canonicalises_builder_transaction_id() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0

    plan = Mock()
    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "AA" * 32,
        b"signed-psbt",
    )

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter",
            return_value=adapter,
        ),
        patch("jmlightning.operations.peerswap.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch("jmlightning.operations.peerswap.TxBuilder", return_value=tx_builder),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=Mock(mixdepth=0),
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        prepared = await operation.execute(request)

    assert prepared.txid == "aa" * 32
    assert list(operation._prepared) == ["aa" * 32]


@pytest.mark.anyio
async def test_txprepare_rejects_invalid_transaction_id_from_builder() -> None:
    request = _request()
    coin = _coin()

    adapter = Mock()
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.get_utxos.return_value = [coin]
    adapter.select_utxos.return_value = [coin.utxo]
    adapter.get_change_address.return_value = PEERSWAP_CHANGE_ADDRESS
    adapter.get_mempool_min_fee = AsyncMock(return_value=None)
    adapter.require_wallet.return_value = Mock()

    cln = Mock()
    cln.get_fee_rate.return_value = 1.0

    plan = Mock()
    tx_builder = Mock()
    tx_builder.build_and_sign_multifunding_tx.return_value = (
        Mock(),
        "not-a-txid",
        b"signed-psbt",
    )

    with (
        patch(
            "jmlightning.operations.peerswap.JoinMarketAdapter",
            return_value=adapter,
        ),
        patch("jmlightning.operations.peerswap.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.peerswap.Planner.build_multi_plan",
            return_value=plan,
        ),
        patch("jmlightning.operations.peerswap.TxBuilder", return_value=tx_builder),
    ):
        operation = PeerSwapPrepareTxOperation(
            config=Mock(mixdepth=0),
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            ValueError,
            match="JoinMarket transaction id must be a 64-character hexadecimal value",
        ):
            await operation.execute(request)

    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_awaited_once()
    assert operation._prepared == {}


def test_txprepare_rejects_boolean_feerate() -> None:
    with pytest.raises(ValueError, match="feerate must be a string or integer"):
        PeerSwapPrepareTxRequest.from_rpc(
            {"outputs": [{PEERSWAP_OUTPUT_ADDRESS: 1000}], "feerate": True}
        )


def test_txprepare_rejects_boolean_amount() -> None:
    with pytest.raises(ValueError, match="Invalid txprepare output amount"):
        PeerSwapPrepareTxRequest.from_rpc(
            {"outputs": [{PEERSWAP_OUTPUT_ADDRESS: True}]}
        )


def test_txprepare_rejects_boolean_minconf() -> None:
    with pytest.raises(ValueError, match="minconf must be a uint32"):
        PeerSwapPrepareTxRequest.from_rpc(
            {"outputs": [{PEERSWAP_OUTPUT_ADDRESS: 1000}], "minconf": True}
        )


@pytest.mark.anyio
async def test_send_rejects_broadcast_transaction_id_mismatch() -> None:
    operation = PeerSwapPrepareTxOperation(Mock(mixdepth=0), Path("/tmp/lightning-rpc"))
    adapter = Mock()
    adapter.broadcast = AsyncMock(return_value="33" * 32)
    adapter.close = AsyncMock()
    coin = _coin()
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"psbt",
    )
    operation._prepared[prepared.txid] = prepared

    with pytest.raises(RuntimeError, match="unexpected transaction id"):
        await operation.send(prepared.txid)

    assert operation._prepared[prepared.txid] is prepared
    assert prepared.phase is PeerSwapPhase.PREPARED
    adapter.close.assert_not_awaited()
    adapter.unlock.assert_not_called()


@pytest.mark.anyio
async def test_send_retains_state_when_unlock_fails() -> None:
    operation = PeerSwapPrepareTxOperation(Mock(mixdepth=0), Path("/tmp/lightning-rpc"))
    adapter = Mock()
    adapter.broadcast = AsyncMock(return_value="22" * 32)
    adapter.close = AsyncMock()
    adapter.unlock.side_effect = RuntimeError("unlock failed")
    coin = _coin()
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"psbt",
    )
    operation._prepared[prepared.txid] = prepared

    with patch(
        "jmlightning.operations.peerswap.serialize_transaction",
        return_value=b"signed-tx",
    ):
        result = await operation.send(prepared.txid)

    assert result["txid"] == prepared.txid
    assert prepared.txid in operation._prepared
    assert prepared.phase is PeerSwapPhase.BROADCAST
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_not_awaited()

    adapter.unlock.side_effect = None
    await operation.close()
    assert operation._prepared == {}
    assert adapter.unlock.call_count == 2
    assert adapter.close.await_count == 1


@pytest.mark.anyio
async def test_send_retains_state_when_close_fails_after_broadcast() -> None:
    operation = PeerSwapPrepareTxOperation(Mock(mixdepth=0), Path("/tmp/lightning-rpc"))
    adapter = Mock()
    adapter.broadcast = AsyncMock(return_value="22" * 32)
    adapter.close = AsyncMock(side_effect=[RuntimeError("close failed"), None])
    coin = _coin()
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"psbt",
    )
    operation._prepared[prepared.txid] = prepared

    with patch(
        "jmlightning.operations.peerswap.serialize_transaction",
        return_value=b"signed-tx",
    ):
        result = await operation.send(prepared.txid)

    assert result["txid"] == prepared.txid
    assert prepared.txid in operation._prepared
    assert prepared.phase is PeerSwapPhase.BROADCAST
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_awaited_once()

    await operation.close()
    assert operation._prepared == {}
    assert adapter.unlock.call_count == 1
    assert adapter.close.await_count == 2


@pytest.mark.anyio
async def test_discard_retains_state_when_close_fails() -> None:
    operation = PeerSwapPrepareTxOperation(Mock(mixdepth=0), Path("/tmp/lightning-rpc"))
    adapter = Mock()
    adapter.close = AsyncMock(side_effect=[RuntimeError("close failed"), None])
    coin = _coin()
    prepared = PreparedPeerSwapTransaction(
        tx=Mock(),
        txid="22" * 32,
        locked=[coin],
        adapter=adapter,
        psbt=b"psbt",
    )
    operation._prepared[prepared.txid] = prepared

    with pytest.raises(RuntimeError, match="Failed to fully clean up"):
        await operation.discard(prepared.txid)

    assert prepared.txid in operation._prepared
    assert prepared.phase is PeerSwapPhase.PREPARED
    adapter.unlock.assert_called_once_with(coin)
    adapter.close.assert_awaited_once()

    with patch(
        "jmlightning.operations.peerswap.PeerSwapPrepareTxOperation._unsigned_tx",
        return_value="02000000",
    ):
        result = await operation.discard(prepared.txid)

    assert result == {"unsigned_tx": "02000000", "txid": prepared.txid}
    assert prepared.txid not in operation._prepared
    assert prepared.phase.value == PeerSwapPhase.DISCARDED.value
    assert adapter.unlock.call_count == 1
    assert adapter.close.await_count == 2
