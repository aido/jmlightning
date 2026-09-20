from unittest.mock import Mock, patch

import pytest

from jmlightning.lightning.backend import ChannelFundingStatus, FeePriority
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


def test_splice_init_calls_rpc_without_optional_psbt_or_feerate() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_init.return_value = {
        "psbt": "cHNidP8=",
    }

    result = backend.splice_init(
        channel_id="11" * 32,
        amount=100_000,
    )

    rpc.splice_init.assert_called_once_with(
        "11" * 32,
        100_000,
        None,
        None,
    )
    assert result == {
        "psbt": "cHNidP8=",
    }


def test_splice_init_passes_optional_psbt_and_feerate() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_init.return_value = {
        "psbt": "cHNidP8=",
    }

    result = backend.splice_init(
        channel_id="11" * 32,
        amount=-50_000,
        initial_psbt=b"test-psbt",
        feerate_per_kw=5_000,
    )

    rpc.splice_init.assert_called_once_with(
        "11" * 32,
        -50_000,
        "dGVzdC1wc2J0",
        5_000,
    )
    assert result == {
        "psbt": "cHNidP8=",
    }


def test_splice_init_rejects_unsupported_force_feerate() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    with pytest.raises(
        RuntimeError,
        match="does not support force_feerate",
    ):
        backend.splice_init(
            channel_id="11" * 32,
            amount=100_000,
            force_feerate=True,
        )

    rpc.splice_init.assert_not_called()


def test_splice_init_rejects_missing_psbt() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_init.return_value = {}

    with pytest.raises(
        RuntimeError,
        match="splice_init response is missing psbt",
    ):
        backend.splice_init(
            channel_id="11" * 32,
            amount=100_000,
        )


def test_splice_update_calls_rpc_and_validates_response() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_update.return_value = {
        "psbt": "cHNidP8=",
        "commitments_secured": True,
        "signatures_secured": False,
    }

    result = backend.splice_update(
        channel_id="11" * 32,
        psbt=b"test-psbt",
    )

    rpc.splice_update.assert_called_once_with(
        "11" * 32,
        "dGVzdC1wc2J0",
    )
    assert result == {
        "psbt": "cHNidP8=",
        "commitments_secured": True,
        "signatures_secured": False,
    }


def test_splice_update_accepts_missing_optional_signatures_secured() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_update.return_value = {
        "psbt": "cHNidP8=",
        "commitments_secured": True,
    }

    result = backend.splice_update(
        channel_id="11" * 32,
        psbt=b"test-psbt",
    )

    rpc.splice_update.assert_called_once_with(
        "11" * 32,
        "dGVzdC1wc2J0",
    )
    assert result == {
        "psbt": "cHNidP8=",
        "commitments_secured": True,
    }


def test_splice_update_rejects_non_dict_response() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_update.return_value = None

    with pytest.raises(
        RuntimeError,
        match="splice_update returned an invalid response",
    ):
        backend.splice_update(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )


def test_splice_update_propagates_rpc_failure() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_update.side_effect = RuntimeError("RPC unavailable")

    with pytest.raises(
        RuntimeError,
        match="RPC unavailable",
    ):
        backend.splice_update(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("psbt", None),
        ("commitments_secured", None),
        ("signatures_secured", "invalid"),
    ],
)
def test_splice_update_rejects_invalid_response_field(
    field: str,
    value: object,
) -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    response: dict[str, object] = {
        "psbt": "cHNidP8=",
        "commitments_secured": False,
        "signatures_secured": False,
    }
    response[field] = value
    rpc.splice_update.return_value = response

    with pytest.raises(
        RuntimeError,
        match=field,
    ):
        backend.splice_update(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )


def test_splice_signed_calls_rpc_and_validates_response() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_signed.return_value = {
        "tx": "02000000",
        "txid": "11" * 32,
        "psbt": "cHNidP8=",
        "outnum": 1,
    }

    result = backend.splice_signed(
        channel_id="11" * 32,
        psbt=b"test-psbt",
    )

    rpc.splice_signed.assert_called_once_with(
        "11" * 32,
        "dGVzdC1wc2J0",
    )
    assert result == {
        "tx": "02000000",
        "txid": "11" * 32,
        "psbt": "cHNidP8=",
        "outnum": 1,
    }


def test_splice_signed_rejects_unsupported_sign_first() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    with pytest.raises(
        RuntimeError,
        match="does not support sign_first",
    ):
        backend.splice_signed(
            channel_id="11" * 32,
            psbt=b"test-psbt",
            sign_first=True,
        )

    rpc.splice_signed.assert_not_called()


@pytest.mark.parametrize("field", ["tx", "txid", "psbt"])
def test_splice_signed_rejects_missing_required_response_field(
    field: str,
) -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    response: dict[str, object] = {
        "tx": "02000000",
        "txid": "11" * 32,
        "psbt": "cHNidP8=",
    }
    response.pop(field)
    rpc.splice_signed.return_value = response

    with pytest.raises(
        RuntimeError,
        match=field,
    ):
        backend.splice_signed(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )


def test_splice_signed_rejects_invalid_outnum() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_signed.return_value = {
        "tx": "02000000",
        "txid": "11" * 32,
        "psbt": "cHNidP8=",
        "outnum": -1,
    }

    with pytest.raises(
        RuntimeError,
        match="invalid outnum",
    ):
        backend.splice_signed(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )


def test_splice_rpc_runtime_errors_are_propagated() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.splice_init.side_effect = RuntimeError("init failed")
    rpc.splice_update.side_effect = RuntimeError("update failed")
    rpc.splice_signed.side_effect = RuntimeError("signed failed")

    with pytest.raises(
        RuntimeError,
        match="init failed",
    ):
        backend.splice_init(
            channel_id="11" * 32,
            amount=100_000,
        )

    with pytest.raises(
        RuntimeError,
        match="update failed",
    ):
        backend.splice_update(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )

    with pytest.raises(
        RuntimeError,
        match="signed failed",
    ):
        backend.splice_signed(
            channel_id="11" * 32,
            psbt=b"test-psbt",
        )


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


@pytest.mark.parametrize(
    "peer_channels",
    [
        [],
        {"channels": "invalid"},
        {"channels": ["invalid"]},
        {"channels": [{"inflight": "invalid"}]},
        {"channels": [{"inflight": ["invalid"]}]},
    ],
)
def test_get_channel_funding_status_rejects_malformed_peer_channel_data(
    peer_channels: object,
) -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.listpeerchannels.return_value = peer_channels

    if peer_channels == []:
        # An empty list is not a valid CLN response object.
        expected = "CLN listpeerchannels returned an invalid response"
    elif isinstance(peer_channels, dict) and peer_channels.get("channels") == "invalid":
        expected = "CLN listpeerchannels returned invalid channels data"
    elif isinstance(peer_channels, dict) and peer_channels.get("channels") == [
        "invalid"
    ]:
        expected = "CLN listpeerchannels returned an invalid channel entry"
    elif isinstance(peer_channels, dict) and peer_channels.get("channels") == [
        {"inflight": "invalid"}
    ]:
        expected = "CLN listpeerchannels returned invalid inflight data"
    else:
        expected = "CLN listpeerchannels returned an invalid inflight channel entry"

    with pytest.raises(RuntimeError, match=expected):
        backend.get_channel_funding_status(
            peer_id="02" + "11" * 32,
            txid="11" * 32,
        )


@pytest.mark.parametrize("transactions", ["invalid", ["invalid"]])
def test_get_channel_funding_status_rejects_malformed_transaction_data(
    transactions: object,
) -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.listpeerchannels.return_value = {"channels": []}
    rpc.listtransactions.return_value = {"transactions": transactions}

    expected = (
        "CLN listtransactions returned invalid transactions data"
        if transactions == "invalid"
        else "CLN listtransactions returned an invalid transaction entry"
    )

    with pytest.raises(RuntimeError, match=expected):
        backend.get_channel_funding_status(
            peer_id="02" + "11" * 32,
            txid="11" * 32,
        )


def test_get_splice_feerate_per_kw_returns_splice_rate() -> None:
    rpc = Mock()
    backend = CLNBackend("/tmp/lightning-rpc")
    backend.rpc = rpc
    rpc.feerates.return_value = {
        "perkw": {
            "splice": 258,
        }
    }

    assert backend.get_splice_feerate_per_kw() == 258
    rpc.feerates.assert_called_once_with(style="perkw")


def test_get_splice_feerate_per_kw_rejects_missing_rate() -> None:
    rpc = Mock()
    backend = CLNBackend("/tmp/lightning-rpc")
    backend.rpc = rpc
    rpc.feerates.return_value = {"perkw": {}}

    with pytest.raises(RuntimeError, match="invalid splice rate"):
        backend.get_splice_feerate_per_kw()


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


def test_get_fee_rate_uses_explicit_cln_feerate() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.parsefeerate.return_value = {"perkw": 11_000}

    assert backend.get_fee_rate(feerate="urgent") == 2.75
    rpc.parsefeerate.assert_called_once_with("urgent")
    rpc.estimatefees.assert_not_called()


def test_get_fee_rate_uses_fee_estimates_without_explicit_feerate() -> None:
    rpc = Mock()

    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": [
            {"blocks": 6, "feerate": 2_000},
        ],
    }

    assert backend.get_fee_rate() == 2.0
    rpc.parsefeerate.assert_not_called()


def test_estimate_fees_validates_and_returns_entries() -> None:
    rpc = Mock()
    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.return_value = {
        "feerates": [
            {"blocks": 2, "feerate": 5_000},
            {"blocks": 6, "feerate": 2_000},
        ]
    }

    estimates = backend._estimate_fees()
    assert [(item.blocks, item.sat_per_kvb) for item in estimates] == [
        (2, 5_000),
        (6, 2_000),
    ]


def test_estimate_fees_rejects_rpc_failure() -> None:
    rpc = Mock()
    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.estimatefees.side_effect = RuntimeError("rpc failed")

    with pytest.raises(RuntimeError, match="Failed to retrieve fee estimates"):
        backend._estimate_fees()


def test_get_fee_rate_rejects_invalid_explicit_cln_rate() -> None:
    rpc = Mock()
    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        backend = CLNBackend("/tmp/lightning-rpc")

    rpc.parsefeerate.return_value = {"perkw": 0}

    with pytest.raises(RuntimeError, match="invalid fee rate"):
        backend.get_fee_rate(
            priority=FeePriority.NORMAL,
            feerate="urgent",
        )
