from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jmwallet.wallet.models import UTXOInfo

from jmlightning.lightning.backend import FeePriority
from jmlightning.models import ClassifiedUTXO, Outpoint
from jmlightning.operations.splice import (
    SpliceInOperation,
    SpliceOutOperation,
    SpliceRecoveryRequiredError,
    confirm_splice_in,
)
from jmlightning.planner import ExecutionPlan
from jmlightning.tx_builder import SpliceContribution


def _coin(txid: str = "11" * 32, vout: int = 0) -> ClassifiedUTXO:
    return ClassifiedUTXO(
        utxo=UTXOInfo(
            txid=txid,
            vout=vout,
            value=200_000,
            mixdepth=0,
            address="bc1qtest",
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path="m/84'/0'/0'/0/0",
        ),
        status="cj-out",
    )


def _build_splice_test_doubles(
    tmp_path: Path,
) -> tuple[
    Mock,
    ClassifiedUTXO,
    Mock,
    Mock,
    ExecutionPlan,
    Mock,
]:
    config = Mock()
    config.data_dir = tmp_path
    config.amount = 100_000
    config.mixdepth = 0
    config.fee_priority = FeePriority.NORMAL

    coin = _coin()

    jmadapter = Mock()
    jmadapter.connect = AsyncMock()
    jmadapter.close = AsyncMock()
    jmadapter.get_utxos.return_value = [coin]
    jmadapter.select_utxos.return_value = [coin.utxo]
    jmadapter.get_change_address.return_value = "bc1qchange"
    jmadapter.require_wallet.return_value = Mock()
    jmadapter.get_raw_transaction = AsyncMock(return_value=b"previous-tx")

    cln = Mock()
    cln.get_splice_feerate_per_kw.return_value = 250
    cln.funding_output_type = "p2wsh"
    cln.splice_init.return_value = {"psbt": "cHNidP8="}
    cln.splice_update.return_value = {
        "psbt": "dXBkYXRlZC1wc2J0",
        "commitments_secured": True,
        "signatures_secured": False,
    }
    cln.splice_signed.return_value = {
        "tx": "02000000",
        "txid": "33" * 32,
        "psbt": "c2lnbmVkLXBzYnQ=",
    }

    plan = ExecutionPlan(
        inputs=[coin],
        amount=100_000,
        fee=100,
        vsize=100,
        change=99_900,
        warnings=[],
        rationale="test plan",
    )

    tx_builder = Mock()
    tx_builder.add_splice_in_input.return_value = (b"candidate-psbt", Mock())
    tx_builder.find_splice_input_index.return_value = 1
    tx_builder.sign_splice_psbt.return_value = (
        Mock(),
        "33" * 32,
        b"signed-jm-psbt",
    )

    return config, coin, jmadapter, cln, plan, tx_builder


@pytest.mark.anyio
async def test_splice_in_zero_amount_sweeps_all_policy_approved_utxos(
    tmp_path: Path,
) -> None:
    config, coin, jmadapter, cln, _plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    config.amount = 0
    second_coin = replace(
        coin,
        utxo=replace(coin.utxo, txid="22" * 32, vout=1, value=300_000),
    )
    jmadapter.get_utxos.return_value = [coin, second_coin]
    jmadapter.get_raw_transaction = AsyncMock(
        side_effect=[b"previous-tx-1", b"previous-tx-2"]
    )
    contribution = SpliceContribution(
        jm_outpoint=coin.outpoint,
        jm_value=coin.utxo.value,
        change_script=None,
        change_value=0,
        channel_output=(0, b"channel"),
        channel_contribution=coin.utxo.value - 500,
        baseline_outputs=(),
        max_fee=500,
    )
    second_contribution = SpliceContribution(
        jm_outpoint=second_coin.outpoint,
        jm_value=second_coin.utxo.value,
        change_script=None,
        change_value=0,
        channel_output=(0, b"channel"),
        channel_contribution=second_coin.utxo.value - 500,
        baseline_outputs=(),
        max_fee=500,
    )
    tx_builder.add_splice_in_input.side_effect = [
        (b"sweep-psbt-1", contribution),
        (b"sweep-psbt-2", second_contribution),
    ]
    tx_builder.find_splice_input_index.side_effect = [1, 2]

    with (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch("jmlightning.operations.splice.CLNBackend", return_value=cln),
        patch("jmlightning.operations.splice.TxBuilder", return_value=tx_builder),
        patch(
            "jmlightning.operations.splice.Planner.build_plan",
            return_value=_plan,
        ),
        patch(
            "jmlightning.operations.splice.estimate_splice_in_fee",
            return_value=(1_000, 1_000),
        ),
    ):
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        await operation.execute("22" * 32)

    jmadapter.select_utxos.assert_not_called()
    assert jmadapter.lock.call_count == 2
    assert cln.splice_init.call_args.kwargs["amount"] == 499_000
    assert tx_builder.add_splice_in_input.call_count == 2
    assert tx_builder.sign_splice_psbt.call_args.kwargs["signing_inputs"] == {
        1: coin,
        2: second_coin,
    }


@pytest.mark.anyio
async def test_splice_in_rejects_negative_amount(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, _plan, _tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    config.amount = -1

    operation = SpliceInOperation(
        config=config,
        cln_socket=Path("/tmp/lightning-rpc"),
    )
    with pytest.raises(ValueError, match="must not be negative"):
        await operation.execute("22" * 32)

    jmadapter.connect.assert_not_awaited()
    cln.splice_init.assert_not_called()


def _build_splice_out_test_doubles(tmp_path: Path) -> tuple[Mock, Mock, Mock]:
    config = Mock()
    config.data_dir = tmp_path
    config.mixdepth = 0

    wallet = Mock()
    wallet.get_new_address_verified = AsyncMock(
        return_value="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"
    )

    jmadapter = Mock()
    jmadapter.connect = AsyncMock()
    jmadapter.close = AsyncMock()
    jmadapter.require_wallet.return_value = wallet

    cln = Mock()
    cln.get_splice_feerate_per_kw.return_value = 10_000
    cln.get_channel_local_balance_sat.return_value = 250_000
    cln.get_channel_capacity_sat.return_value = 500_000
    cln.add_psbt_output.return_value = {"psbt": "b3V0cHV0LXBzYnQ="}
    cln.splice_init.return_value = {"psbt": "aW5pdC1wc2J0"}
    cln.splice_update.return_value = {
        "psbt": "dXBkYXRlZC1wc2J0",
        "commitments_secured": True,
    }
    cln.splice_signed.return_value = {
        "tx": "02000000",
        "txid": "44" * 32,
        "psbt": "c2lnbmVkLXBzYnQ=",
    }
    return config, jmadapter, cln


@pytest.mark.anyio
async def test_splice_out_uses_cln_output_and_pays_initiator_fee(
    tmp_path: Path,
) -> None:
    config, jmadapter, cln = _build_splice_out_test_doubles(tmp_path)

    with (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch("jmlightning.operations.splice.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.splice.estimate_splice_out_fee",
            return_value=(321, 579),
        ),
        patch.object(
            SpliceOutOperation,
            "_validate_splice_out_output",
            return_value=None,
        ),
    ):
        operation = SpliceOutOperation(config, Path("/tmp/lightning-rpc"))
        txid = await operation.execute("22" * 32, 100_000)

    assert txid == "44" * 32
    cln.add_psbt_output.assert_called_once_with(
        100_000, "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"
    )
    cln.splice_init.assert_called_once_with(
        channel_id="22" * 32,
        amount=-100_321,
        initial_psbt=b"output-psbt",
        feerate_per_kw=10_000,
    )
    cln.splice_update.assert_called_once()
    cln.splice_signed.assert_called_once()


@pytest.mark.anyio
async def test_splice_out_zero_amount_sweeps_local_balance_after_fee(
    tmp_path: Path,
) -> None:
    config, jmadapter, cln = _build_splice_out_test_doubles(tmp_path)

    with (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch("jmlightning.operations.splice.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.splice.estimate_splice_out_fee",
            return_value=(321, 579),
        ),
        patch.object(
            SpliceOutOperation,
            "_validate_splice_out_output",
            return_value=None,
        ),
    ):
        operation = SpliceOutOperation(config, Path("/tmp/lightning-rpc"))
        txid = await operation.execute("22" * 32, 0)

    assert txid == "44" * 32
    cln.get_channel_local_balance_sat.assert_called_once_with("22" * 32)
    assert cln.add_psbt_output.call_args_list[0].args[0] == 250_000
    assert cln.add_psbt_output.call_args_list[1].args[0] == 249_679
    assert cln.splice_init.call_args.kwargs["amount"] == -250_000


@pytest.mark.anyio
async def test_splice_out_sweep_rejects_balance_insufficient_for_fee(
    tmp_path: Path,
) -> None:
    config, jmadapter, cln = _build_splice_out_test_doubles(tmp_path)
    cln.get_channel_local_balance_sat.return_value = 321

    with (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch("jmlightning.operations.splice.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.splice.estimate_splice_out_fee",
            return_value=(321, 579),
        ),
    ):
        operation = SpliceOutOperation(config, Path("/tmp/lightning-rpc"))
        with pytest.raises(ValueError, match="insufficient to pay the splice fee"):
            await operation.execute("22" * 32, 0)

    cln.splice_init.assert_not_called()


@pytest.mark.anyio
async def test_splice_out_rejects_negative_amount(tmp_path: Path) -> None:
    config, jmadapter, cln = _build_splice_out_test_doubles(tmp_path)

    operation = SpliceOutOperation(config, Path("/tmp/lightning-rpc"))
    with pytest.raises(ValueError, match="must not be negative"):
        await operation.execute("22" * 32, -1)

    jmadapter.connect.assert_not_awaited()
    cln.add_psbt_output.assert_not_called()


@pytest.mark.anyio
async def test_splice_out_confirmation_decline_does_not_sign(tmp_path: Path) -> None:
    config, jmadapter, cln = _build_splice_out_test_doubles(tmp_path)

    with (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch("jmlightning.operations.splice.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.splice.estimate_splice_out_fee",
            return_value=(321, 579),
        ),
        patch.object(
            SpliceOutOperation,
            "_validate_splice_out_output",
            return_value=None,
        ),
    ):
        operation = SpliceOutOperation(config, Path("/tmp/lightning-rpc"))
        with pytest.raises(SpliceRecoveryRequiredError, match="declined"):
            await operation.execute(
                "22" * 32,
                100_000,
                confirm=lambda *_args: False,
            )

    cln.splice_signed.assert_not_called()


@pytest.mark.anyio
async def test_splice_out_init_failure_requires_recovery(tmp_path: Path) -> None:
    config, jmadapter, cln = _build_splice_out_test_doubles(tmp_path)
    cln.splice_init.side_effect = RuntimeError("connection lost")

    with (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch("jmlightning.operations.splice.CLNBackend", return_value=cln),
        patch(
            "jmlightning.operations.splice.estimate_splice_out_fee",
            return_value=(321, 579),
        ),
    ):
        operation = SpliceOutOperation(config, Path("/tmp/lightning-rpc"))
        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="splice_init outcome is unknown",
        ):
            await operation.execute("22" * 32, 100_000)

    cln.splice_update.assert_not_called()
    cln.splice_signed.assert_not_called()


def _patch_splice_doubles(
    jmadapter: Mock,
    cln: Mock,
    plan: ExecutionPlan,
    tx_builder: Mock,
) -> tuple[Any, Any, Any, Any, Any]:
    return (
        patch(
            "jmlightning.operations.splice.JoinMarketAdapter",
            return_value=jmadapter,
        ),
        patch(
            "jmlightning.operations.splice.CLNBackend",
            return_value=cln,
        ),
        patch(
            "jmlightning.operations.splice.TxBuilder",
            return_value=tx_builder,
        ),
        patch(
            "jmlightning.operations.splice.estimate_splice_in_fee",
            return_value=(100, 100),
        ),
        patch(
            "jmlightning.operations.splice.Planner.build_plan",
            return_value=plan,
        ),
    )


@pytest.mark.anyio
async def test_selection_requires_exactly_one_utxo(tmp_path: Path) -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    second_coin = replace(
        coin,
        utxo=replace(coin.utxo, txid="22" * 32, vout=1),
    )
    jmadapter.get_utxos.return_value = [coin, second_coin]
    jmadapter.select_utxos.return_value = [coin.utxo, second_coin.utxo]

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            RuntimeError,
            match="Splice-in requires exactly one policy-approved JoinMarket UTXO",
        ):
            await operation.execute("22" * 32)

    jmadapter.lock.assert_not_called()
    cln.splice_init.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_init_failure_keeps_utxo_locked(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_init.side_effect = RuntimeError("connection lost")

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="splice_init outcome is unknown",
        ):
            await operation.execute("22" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_init_invalid_response_keeps_utxo_locked(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_init.return_value = {"psbt": ""}

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="invalid PSBT",
        ):
            await operation.execute("22" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_local_failure_after_splice_init_keeps_utxo_locked(
    tmp_path: Path,
) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    jmadapter.get_change_address.side_effect = RuntimeError("change address failed")

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="change address failed"):
            await operation.execute("22" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_update_failure_requires_recovery(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_update.side_effect = RuntimeError("update failed")

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="splice_update outcome is unknown",
        ) as exc_info:
            await operation.execute("22" * 32)

    assert exc_info.value.locked_outpoints == (Outpoint("11" * 32, 0),)
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_update_repeats_until_commitments_secured(tmp_path: Path) -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_update.side_effect = [
        {
            "psbt": "Zmlyc3QtdXBkYXRlZA==",
            "commitments_secured": False,
            "signatures_secured": False,
        },
        {
            "psbt": "c2Vjb25kLXVwZGF0ZWQ=",
            "commitments_secured": True,
            "signatures_secured": False,
        },
    ]

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        await operation.execute("22" * 32)

    assert tx_builder.validate_splice_psbt.call_count == 4
    assert cln.splice_update.call_count == 2
    assert cln.splice_update.call_args_list[0].kwargs["psbt"] == b"candidate-psbt"
    assert cln.splice_update.call_args_list[1].kwargs["psbt"] == b"first-updated"
    tx_builder.find_splice_input_index.assert_called_once_with(
        psbt=b"second-updated",
        coin=coin,
    )
    tx_builder.sign_splice_psbt.assert_called_once_with(
        psbt=b"second-updated",
        signing_inputs={1: coin},
        wallet=jmadapter.require_wallet.return_value,
    )
    cln.splice_signed.assert_called_once_with(
        channel_id="22" * 32,
        psbt=b"signed-jm-psbt",
    )
    jmadapter.lock.assert_called_once_with(coin)
    jmadapter.get_raw_transaction.assert_awaited_once_with(coin.utxo.txid)
    jmadapter.unlock.assert_not_called()


@pytest.mark.anyio
async def test_tx_builder_failure_after_splice_init_keeps_utxo_locked(
    tmp_path: Path,
) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    tx_builder.add_splice_in_input.side_effect = RuntimeError(
        "transaction build failed"
    )

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(RuntimeError, match="transaction build failed"):
            await operation.execute("22" * 32)

    cln.splice_update.assert_not_called()
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_confirmation_happens_before_splice_signed(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    events: list[str] = []

    def confirm(channel_id: str, received_plan: object, psbt: bytes) -> bool:
        assert channel_id == "22" * 32
        assert isinstance(received_plan, ExecutionPlan)
        assert received_plan is not plan
        assert received_plan.fee == 100
        assert received_plan.change == 99_900
        assert received_plan.vsize == 25
        assert psbt == b"updated-psbt"
        events.append("confirm")
        return True

    def sign_splice_psbt(**kwargs: object) -> tuple[Mock, str, bytes]:
        events.append("sign")
        return Mock(), "33" * 32, b"signed-jm-psbt"

    tx_builder.sign_splice_psbt.side_effect = sign_splice_psbt

    def splice_signed(**kwargs: object) -> dict[str, object]:
        events.append("splice_signed")
        return {
            "psbt": "c2lnbmVkLXBzYnQ=",
            "tx": "02000000",
            "txid": "33" * 32,
        }

    cln.splice_signed.side_effect = splice_signed

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        await operation.execute("22" * 32, confirm=confirm)

    assert events == ["confirm", "sign", "splice_signed"]


@pytest.mark.anyio
async def test_confirmation_rejection_prevents_splice_signed(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )

    def confirm(channel_id: str, received_plan: object, psbt: bytes) -> bool:
        return False

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="splice-in declined",
        ):
            await operation.execute("22" * 32, confirm=confirm)

    cln.splice_signed.assert_not_called()
    jmadapter.unlock.assert_not_called()


@pytest.mark.anyio
async def test_confirmation_receives_actual_plan_and_psbt(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    captured: dict[str, object] = {}

    def confirm(channel_id: str, received_plan: object, psbt: bytes) -> bool:
        captured["channel_id"] = channel_id
        captured["plan"] = received_plan
        captured["psbt"] = psbt
        return True

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        await operation.execute("22" * 32, confirm=confirm)

    received_plan = captured["plan"]
    assert isinstance(received_plan, ExecutionPlan)
    assert received_plan is not plan
    assert received_plan.fee == 100
    assert received_plan.change == 99_900
    assert received_plan.vsize == 25
    assert plan.vsize != received_plan.vsize
    assert captured["channel_id"] == "22" * 32
    assert captured["psbt"] == b"updated-psbt"


@pytest.mark.anyio
async def test_splice_signing_failure_requires_recovery(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    tx_builder.sign_splice_psbt.side_effect = RuntimeError("signing failed")

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="Unable to sign the splice PSBT",
        ) as exc_info:
            await operation.execute("22" * 32)

    assert exc_info.value.locked_outpoints == (Outpoint("11" * 32, 0),)
    cln.splice_signed.assert_not_called()
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_successful_splice_keeps_inputs_locked(tmp_path: Path) -> None:
    config, coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4] as mock_build_plan,
    ):
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        await operation.execute("22" * 32)

    cln.splice_init.assert_called_once_with(
        channel_id="22" * 32,
        amount=config.amount,
        feerate_per_kw=250,
    )
    mock_build_plan.assert_called_once_with(
        selected_coins=[coin],
        target_amount=config.amount,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )
    cln.splice_update.assert_called_once_with(
        channel_id="22" * 32,
        psbt=b"candidate-psbt",
    )
    tx_builder.find_splice_input_index.assert_called_once_with(
        psbt=b"updated-psbt",
        coin=coin,
    )
    tx_builder.sign_splice_psbt.assert_called_once_with(
        psbt=b"updated-psbt",
        signing_inputs={1: coin},
        wallet=jmadapter.require_wallet.return_value,
    )
    cln.splice_signed.assert_called_once_with(
        channel_id="22" * 32,
        psbt=b"signed-jm-psbt",
    )
    jmadapter.lock.assert_called_once_with(coin)
    jmadapter.close.assert_awaited_once()
    jmadapter.unlock.assert_not_called()


@pytest.mark.anyio
async def test_successful_splice_survives_close_failure(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    jmadapter.close.side_effect = RuntimeError("close failed")

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )
        await operation.execute("22" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_signed_failure_requires_recovery(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_signed.side_effect = RuntimeError("signed failed")

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="splice_signed outcome is unknown",
        ) as exc_info:
            await operation.execute("22" * 32)

    assert exc_info.value.channel_id == "22" * 32
    assert exc_info.value.locked_outpoints == (Outpoint("11" * 32, 0),)
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_signed_mismatched_txid_requires_recovery(tmp_path: Path) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_signed.return_value = {
        "tx": "02000000",
        "txid": "44" * 32,
        "psbt": "c2lnbmVkLXBzYnQ=",
    }

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="transaction id that does not match",
        ) as exc_info:
            await operation.execute("22" * 32)

    assert exc_info.value.txid == "33" * 32
    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


@pytest.mark.anyio
async def test_splice_signed_invalid_transaction_requires_recovery(
    tmp_path: Path,
) -> None:
    config, _coin, jmadapter, cln, plan, tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    cln.splice_signed.return_value = {
        "tx": "not-hex",
        "txid": "22" * 32,
        "psbt": "c2lnbmVkLXBzYnQ=",
    }

    patches = _patch_splice_doubles(jmadapter, cln, plan, tx_builder)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        operation = SpliceInOperation(
            config=config,
            cln_socket=Path("/tmp/lightning-rpc"),
        )

        with pytest.raises(
            SpliceRecoveryRequiredError,
            match="CLN splice_signed returned a transaction id that "
            "does not match the signed JoinMarket transaction",
        ):
            await operation.execute("22" * 32)

    jmadapter.unlock.assert_not_called()
    jmadapter.close.assert_awaited_once()


def test_confirm_splice_in_displays_plan_and_accepts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _config, coin, _jmadapter, _cln, plan, _tx_builder = _build_splice_test_doubles(
        tmp_path
    )
    plan.inputs = [coin]
    plan.amount = 100_000
    plan.fee = 100
    plan.vsize = 140
    plan.change = 99_900
    plan.warnings = ["high fee rate"]

    monkeypatch.setattr(
        "jmlightning.operations.splice.typer.confirm",
        lambda message, default=False: True,
    )

    assert (
        confirm_splice_in(
            channel_id="22" * 32,
            plan=plan,
            psbt=b"updated-psbt",
        )
        is True
    )

    output = capsys.readouterr().out
    assert "Channel splice-in" in output
    assert "Channel ID:" in output
    assert "Splice-in amount:  100,000 sats" in output
    assert "Fee:               100 sats" in output
    assert "Virtual size:      140 vbytes" in output
    assert "Change:            99,900 sats" in output
    assert "JoinMarket inputs:" in output
    assert f"{coin.utxo.txid}:{coin.utxo.vout}" in output
    assert "Warnings:" in output
    assert "WARNING: high fee rate" in output
    assert "Negotiated PSBT: 12 bytes" in output
