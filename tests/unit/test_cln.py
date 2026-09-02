from unittest.mock import Mock, patch

import pytest

from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.lightning.cln import CLNBackend


def test_cln_backend_creates_rpc_client() -> None:
    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=Mock(),
    ) as rpc_class:
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc_class.assert_called_once_with("/tmp/lightning-rpc")
    assert backend.rpc is rpc_class.return_value


def test_cln_backend_uses_p2wsh_funding_output() -> None:
    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=Mock(),
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    assert backend.funding_output_type == "p2wsh"


def test_open_channel_start_calls_fundchannel_start() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.fundchannel_start.return_value = {
        "funding_address": "bc1qexample",
    }

    result = backend.open_channel_start(
        peer_id="02" + "11" * 32,
        amount=150_000,
        announce=False,
    )

    rpc.fundchannel_start.assert_called_once_with(
        "02" + "11" * 32,
        150_000,
        announce=False,
    )
    assert result == "bc1qexample"


def test_open_channel_start_rejects_missing_funding_address() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.fundchannel_start.return_value = {}

    with pytest.raises(
        RuntimeError,
        match="funding_address",
    ):
        backend.open_channel_start(
            peer_id="02" + "11" * 32,
            amount=150_000,
            announce=False,
        )


def test_open_channel_complete_calls_fundchannel_complete() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.fundchannel_complete.return_value = {
        "commitments_secured": True,
    }

    psbt = b"test-psbt"

    result = backend.open_channel_complete(
        peer_id="02" + "11" * 32,
        psbt=psbt,
    )

    rpc.fundchannel_complete.assert_called_once_with(
        node_id="02" + "11" * 32,
        psbt="dGVzdC1wc2J0",
        withhold=True,
    )

    rpc.fundchannel_cancel.assert_not_called()

    assert result == {
        "commitments_secured": True,
    }


def test_open_channel_complete_does_not_cancel_on_failure() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.fundchannel_complete.side_effect = RuntimeError("completion failed")

    with pytest.raises(
        RuntimeError,
        match="Failed to complete channel open",
    ):
        backend.open_channel_complete(
            peer_id="02" + "11" * 32,
            psbt=b"test-psbt",
        )

    rpc.fundchannel_complete.assert_called_once_with(
        node_id="02" + "11" * 32,
        psbt="dGVzdC1wc2J0",
        withhold=True,
    )

    rpc.fundchannel_cancel.assert_not_called()


def test_open_channel_complete_preserves_original_error_if_cancel_succeeds() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    original_error = RuntimeError("completion failed")
    rpc.fundchannel_complete.side_effect = original_error

    with pytest.raises(
        RuntimeError,
        match="Failed to complete channel open",
    ) as exc_info:
        backend.open_channel_complete(
            peer_id="02" + "11" * 32,
            psbt=b"test-psbt",
        )

    assert exc_info.value.__cause__ is original_error


def test_open_channel_complete_preserves_completion_error_if_cancel_fails() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    completion_error = RuntimeError("completion failed")
    cancel_error = RuntimeError("cancel failed")

    rpc.fundchannel_complete.side_effect = completion_error
    rpc.fundchannel_cancel.side_effect = cancel_error

    with pytest.raises(
        RuntimeError,
        match="Failed to complete channel open",
    ) as exc_info:
        backend.open_channel_complete(
            peer_id="02" + "11" * 32,
            psbt=b"test-psbt",
        )

    assert exc_info.value.__cause__ is completion_error


def test_cancel_channel_funding_calls_fundchannel_cancel() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    backend.cancel_channel_funding(
        peer_id="02" + "11" * 32,
    )

    rpc.fundchannel_cancel.assert_called_once_with(
        node_id="02" + "11" * 32,
    )


def test_get_channel_funding_status_reports_withheld() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    peer_id = "02" + "11" * 32
    txid = "11" * 32

    rpc.listpeerchannels.return_value = {
        "channels": [
            {
                "peer_id": peer_id,
                "funding_txid": txid,
                "funding": {"withheld": True},
            }
        ]
    }

    assert (
        backend.get_channel_funding_status(
            peer_id=peer_id,
            txid=txid,
        )
        is ChannelFundingStatus.WITHHELD
    )

    rpc.listpeerchannels.assert_called_once_with(peer_id)
    rpc.listtransactions.assert_not_called()


def test_get_channel_funding_status_reports_broadcast_channel() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    peer_id = "02" + "11" * 32
    txid = "11" * 32

    rpc.listpeerchannels.return_value = {
        "channels": [
            {
                "peer_id": peer_id,
                "funding_txid": txid,
                "funding": {"withheld": False},
            }
        ]
    }

    assert (
        backend.get_channel_funding_status(
            peer_id=peer_id,
            txid=txid,
        )
        is ChannelFundingStatus.BROADCAST
    )

    rpc.listpeerchannels.assert_called_once_with(peer_id)


def test_get_channel_funding_status_reports_broadcast_transaction() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    txid = "11" * 32

    rpc.listpeerchannels.return_value = {"channels": []}
    rpc.listtransactions.return_value = {
        "transactions": [{"hash": txid}],
    }

    assert (
        backend.get_channel_funding_status(
            peer_id="02" + "11" * 32,
            txid=txid,
        )
        is ChannelFundingStatus.BROADCAST
    )


def test_get_channel_funding_status_reports_absent() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.listpeerchannels.return_value = {"channels": []}
    rpc.listtransactions.return_value = {"transactions": []}

    assert (
        backend.get_channel_funding_status(
            peer_id="02" + "11" * 32,
            txid="11" * 32,
        )
        is ChannelFundingStatus.ABSENT
    )


def test_get_channel_funding_status_raises_if_state_cannot_be_read() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.listpeerchannels.side_effect = RuntimeError("rpc unavailable")

    with pytest.raises(
        RuntimeError,
        match="Failed to determine CLN funding status",
    ):
        backend.get_channel_funding_status(
            peer_id="02" + "11" * 32,
            txid="11" * 32,
        )


def test_get_fee_rate_rejects_empty_fee_estimates() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": [],
    }

    with pytest.raises(
        RuntimeError,
        match="invalid or empty feerates data",
    ):
        backend.get_fee_rate()


def test_get_fee_rate_rejects_malformed_response() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": "invalid",
    }

    with pytest.raises(
        RuntimeError,
        match="invalid or empty feerates data",
    ):
        backend.get_fee_rate()


def test_get_fee_rate_rejects_malformed_fee_entry() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": [
            {
                "blocks": 6,
            },
        ],
    }

    with pytest.raises(
        RuntimeError,
        match="invalid fee rate",
    ):
        backend.get_fee_rate()


def test_get_fee_rate_rejects_invalid_confirmation_target() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": [
            {
                "blocks": 0,
                "feerate": 1000,
            },
        ],
    }

    with pytest.raises(
        RuntimeError,
        match="invalid confirmation target",
    ):
        backend.get_fee_rate()


def test_get_fee_rate_rejects_non_positive_fee_rate() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": [
            {
                "blocks": 6,
                "feerate": 0,
            },
        ],
    }

    with pytest.raises(
        RuntimeError,
        match="invalid fee rate",
    ):
        backend.get_fee_rate()
