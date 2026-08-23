from unittest.mock import Mock, patch

import pytest

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
