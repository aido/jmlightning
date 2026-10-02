from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from jmwallet.wallet.psbt import ParsedPSBT

from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.lightning.cln import (
    CLN_PSBT_SERIAL_ID_KEY,
    CLNBackend,
    _input_weight,
    serial_ids,
)


def make_backend(rpc: Mock) -> CLNBackend:
    with patch(
        "jmlightning.lightning.cln.LightningRpc",
        return_value=rpc,
    ):
        return CLNBackend("/tmp/lightning-rpc")


def test_serial_ids_rejects_invalid_serial_id_length() -> None:
    record = SimpleNamespace(key=CLN_PSBT_SERIAL_ID_KEY, value=b"\x01")
    parsed = SimpleNamespace(
        input_maps=[SimpleNamespace(records=[record])], output_maps=[]
    )

    with pytest.raises(ValueError, match="Invalid Core Lightning serial ID"):
        serial_ids(cast(ParsedPSBT, parsed))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (b"\x01", "Invalid witness UTXO record"),
        (b"\x01" * 9, "Invalid witness UTXO record"),
    ],
)
def test_input_weight_rejects_malformed_witness_utxo(
    value: bytes, message: str
) -> None:
    record = SimpleNamespace(key=b"\x01", value=value)
    parsed = SimpleNamespace(
        input_maps=[SimpleNamespace(records=[record])],
        transaction=SimpleNamespace(inputs=[SimpleNamespace(vout=0)]),
    )

    with pytest.raises(ValueError, match=message):
        _input_weight(cast(ParsedPSBT, parsed), 0)


def test_input_weight_rejects_invalid_non_witness_vout() -> None:
    record = SimpleNamespace(key=b"\x00", value=b"not-a-transaction")
    parsed = SimpleNamespace(
        input_maps=[SimpleNamespace(records=[record])],
        transaction=SimpleNamespace(inputs=[SimpleNamespace(vout=1)]),
    )
    previous = SimpleNamespace(
        outputs=[SimpleNamespace(script=b"\x00\x14" + b"\x00" * 20)]
    )

    with (
        patch(
            "jmlightning.lightning.cln.parse_transaction_bytes",
            return_value=previous,
        ),
        pytest.raises(ValueError, match="Invalid non-witness UTXO record"),
    ):
        _input_weight(cast(ParsedPSBT, parsed), 0)


def test_input_weight_rejects_missing_utxo_data() -> None:
    parsed = SimpleNamespace(
        input_maps=[SimpleNamespace(records=[])],
        transaction=SimpleNamespace(inputs=[SimpleNamespace(vout=0)]),
    )

    with pytest.raises(ValueError, match="missing UTXO data"):
        _input_weight(cast(ParsedPSBT, parsed), 0)


def test_input_weight_rejects_unsupported_script_type() -> None:
    record = SimpleNamespace(
        key=b"\x01",
        value=b"\x00" * 8 + b"\x01" + b"\x6a",
    )
    parsed = SimpleNamespace(
        input_maps=[SimpleNamespace(records=[record])],
        transaction=SimpleNamespace(inputs=[SimpleNamespace(vout=0)]),
    )

    with pytest.raises(ValueError, match="Unsupported splice input script type"):
        _input_weight(cast(ParsedPSBT, parsed), 0)


def test_open_channel_complete_rejects_invalid_response_shape() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.fundchannel_complete.return_value = None

    with pytest.raises(RuntimeError, match="invalid response"):
        backend.open_channel_complete("02" + "11" * 32, b"test-psbt")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"channel_id": 1, "commitments_secured": True}, "invalid channel_id"),
        ({"channel_id": "", "commitments_secured": True}, "invalid channel_id"),
        ({"commitments_secured": False}, "invalid commitments_secured"),
        ({"commitments_secured": 1}, "invalid commitments_secured"),
    ],
)
def test_open_channel_complete_rejects_invalid_typed_result(
    response: object, message: str
) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.fundchannel_complete.return_value = response

    with pytest.raises(RuntimeError, match=message):
        backend.open_channel_complete("02" + "11" * 32, b"test-psbt")


def test_add_psbt_output_returns_typed_result() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.addpsbtoutput.return_value = {
        "psbt": "cHNidP8=",
        "estimated_added_weight": 124,
        "outnum": 2,
    }

    result = backend.add_psbt_output(
        amount=100_000,
        destination="bcrt1qexample",
        initial_psbt=b"test-psbt",
    )

    rpc.addpsbtoutput.assert_called_once_with(
        satoshi=100_000,
        initialpsbt="dGVzdC1wc2J0",
        destination="bcrt1qexample",
    )
    assert result == {
        "psbt": "cHNidP8=",
        "estimated_added_weight": 124,
        "outnum": 2,
    }


@pytest.mark.parametrize(
    ("amount", "destination", "message"),
    [(0, "bcrt1qexample", "amount"), (100_000, "", "destination")],
)
def test_add_psbt_output_rejects_invalid_arguments(
    amount: int, destination: str, message: str
) -> None:
    rpc = Mock()
    backend = make_backend(rpc)

    with pytest.raises(ValueError, match=message):
        backend.add_psbt_output(amount, destination)

    rpc.addpsbtoutput.assert_not_called()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (None, "invalid response"),
        ({"estimated_added_weight": 124, "outnum": 0}, "missing psbt"),
        (
            {"psbt": "cHNidP8=", "estimated_added_weight": -1, "outnum": 0},
            "invalid estimated_added_weight",
        ),
        (
            {"psbt": "cHNidP8=", "estimated_added_weight": 124, "outnum": -1},
            "invalid outnum",
        ),
    ],
)
def test_add_psbt_output_rejects_invalid_response(
    response: object, message: str
) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.addpsbtoutput.return_value = response

    with pytest.raises(RuntimeError, match=message):
        backend.add_psbt_output(100_000, "bcrt1qexample")


def test_add_psbt_output_translates_rpc_error() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.addpsbtoutput.side_effect = RuntimeError("wallet unavailable")

    with pytest.raises(RuntimeError, match="wallet unavailable"):
        backend.add_psbt_output(100_000, "bcrt1qexample")


def test_get_funding_start_status_reports_withheld() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = {
        "channels": [{"inflight": [{"funding_txid": "11" * 32}]}],
    }

    assert (
        backend.get_funding_start_status("02" + "11" * 32)
        is ChannelFundingStatus.WITHHELD
    )


def test_get_funding_start_status_reports_absent_for_empty_inflight() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = {
        "channels": [{"inflight": []}, {"inflight": None}, "invalid"],
    }

    assert (
        backend.get_funding_start_status("02" + "11" * 32)
        is ChannelFundingStatus.ABSENT
    )


@pytest.mark.parametrize("response", [None, {"channels": "invalid"}])
def test_get_funding_start_status_rejects_invalid_response(response: object) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = response

    with pytest.raises(
        RuntimeError, match="Failed to determine CLN funding-start status"
    ):
        backend.get_funding_start_status("02" + "11" * 32)


def test_get_funding_start_status_translates_rpc_error() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.side_effect = RuntimeError("rpc unavailable")

    with pytest.raises(
        RuntimeError, match="Failed to determine CLN funding-start status"
    ):
        backend.get_funding_start_status("02" + "11" * 32)


def test_get_splice_funding_status_reports_absent() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = {
        "channels": [{"inflight": []}, {"inflight": None}, "invalid"],
    }

    assert backend.get_splice_funding_status("11" * 32) is ChannelFundingStatus.ABSENT


@pytest.mark.parametrize("response", [None, {"channels": "invalid"}])
def test_get_splice_funding_status_rejects_invalid_response(response: object) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = response

    with pytest.raises(RuntimeError, match="Failed to determine CLN splice status"):
        backend.get_splice_funding_status("11" * 32)


def test_send_psbt_returns_typed_result() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.sendpsbt.return_value = {"tx": "02000000", "txid": "11" * 32}

    result = backend.send_psbt(b"test-psbt")

    rpc.sendpsbt.assert_called_once_with(psbt="dGVzdC1wc2J0")
    assert result == {"tx": "02000000", "txid": "11" * 32}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (None, "invalid response"),
        ({"txid": "11" * 32}, "missing tx"),
        ({"tx": "02000000"}, "missing txid"),
        ({"tx": "", "txid": "11" * 32}, "missing tx"),
        ({"tx": "02000000", "txid": ""}, "missing txid"),
    ],
)
def test_send_psbt_rejects_invalid_response(response: object, message: str) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.sendpsbt.return_value = response

    with pytest.raises(RuntimeError, match=message):
        backend.send_psbt(b"test-psbt")


def test_get_channel_local_balance_sat_rejects_invalid_rpc_shape() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = None

    with pytest.raises(RuntimeError, match="invalid response"):
        backend.get_channel_local_balance_sat("11" * 32)


@pytest.mark.parametrize(
    "channels",
    [
        "invalid",
        [{"channel_id": "22" * 32, "to_us_msat": -1}],
        [{"channel_id": "22" * 32, "to_us_msat": True}],
        [{"channel_id": "22" * 32, "to_us_msat": "250000"}],
        [{"channel_id": "33" * 32, "to_us_msat": 250_000}],
    ],
)
def test_get_channel_local_balance_sat_rejects_invalid_channel_data(
    channels: object,
) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = {"channels": channels}

    with pytest.raises(RuntimeError):
        backend.get_channel_local_balance_sat("22" * 32)


def test_get_channel_local_balance_sat_translates_rpc_error() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.side_effect = RuntimeError("rpc unavailable")

    with pytest.raises(RuntimeError, match="Failed to retrieve channel balance"):
        backend.get_channel_local_balance_sat("11" * 32)


@pytest.mark.parametrize("response", [None, {"channels": "invalid"}])
def test_get_channel_capacity_sat_rejects_invalid_response(response: object) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = response

    with pytest.raises(RuntimeError, match="invalid channel data"):
        backend.get_channel_capacity_sat("11" * 32)


@pytest.mark.parametrize(
    "channels",
    [
        [{"channel_id": "22" * 32, "amount_msat": -1}],
        [{"channel_id": "22" * 32, "amount_msat": True}],
        [{"channel_id": "22" * 32, "amount_msat": "250000"}],
        [{"channel_id": "33" * 32, "amount_msat": 250_000}],
    ],
)
def test_get_channel_capacity_sat_rejects_invalid_channel_data(
    channels: object,
) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.return_value = {"channels": channels}

    with pytest.raises(RuntimeError):
        backend.get_channel_capacity_sat("22" * 32)


def test_get_channel_capacity_sat_translates_rpc_error() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.listpeerchannels.side_effect = RuntimeError("rpc unavailable")

    with pytest.raises(RuntimeError, match="Failed to retrieve channel capacity"):
        backend.get_channel_capacity_sat("11" * 32)


@pytest.mark.parametrize(
    "response",
    [None, {"perkw": None}, {"perkw": {"splice": 0}}, {"perkw": {"splice": True}}],
)
def test_get_splice_feerate_per_kw_rejects_invalid_response(response: object) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.feerates.return_value = response

    with pytest.raises(RuntimeError):
        backend.get_splice_feerate_per_kw()


def test_get_splice_feerate_per_kw_translates_rpc_error() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.feerates.side_effect = RuntimeError("rpc unavailable")

    with pytest.raises(RuntimeError, match="Failed to retrieve CLN splice feerate"):
        backend.get_splice_feerate_per_kw()


def test_estimate_fees_rejects_non_dict_response() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.estimatefees.return_value = None

    with pytest.raises(RuntimeError, match="estimatefees returned an invalid response"):
        backend._estimate_fees()


def test_estimate_fees_rejects_non_dict_entry() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.estimatefees.return_value = {"feerates": ["invalid"]}

    with pytest.raises(RuntimeError, match="invalid fee estimate entry"):
        backend._estimate_fees()


@pytest.mark.parametrize(
    "entry",
    [
        {"blocks": True, "feerate": 1000},
        {"blocks": -1, "feerate": 1000},
        {"blocks": 6, "feerate": True},
        {"blocks": 6, "feerate": -1},
    ],
)
def test_estimate_fees_rejects_invalid_fee_values(entry: dict[str, object]) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.estimatefees.return_value = {"feerates": [entry]}

    with pytest.raises(RuntimeError):
        backend._estimate_fees()


def test_get_fee_rate_translates_explicit_parse_error() -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.parsefeerate.side_effect = RuntimeError("bad feerate")

    with pytest.raises(RuntimeError, match="Failed to parse CLN feerate: bad feerate"):
        backend.get_fee_rate(feerate="urgent")


@pytest.mark.parametrize(
    "response",
    [None, {"perkw": True}, {"perkw": 0}, {"perkw": -1}],
)
def test_get_fee_rate_rejects_invalid_explicit_parse_response(response: object) -> None:
    rpc = Mock()
    backend = make_backend(rpc)
    rpc.parsefeerate.return_value = response

    with pytest.raises(RuntimeError, match="invalid"):
        backend.get_fee_rate(feerate="urgent")
