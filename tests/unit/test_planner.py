import math

import pytest
from jmcore.bitcoin import estimate_vsize

from jmlightning.models import ClassifiedUTXO
from jmlightning.planner import Planner


def test_build_plan_calculates_amount_fee_and_change(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    plan = planner.build_plan(
        selected_coins=selected,
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    assert plan.inputs == selected
    assert plan.amount == 150_000
    assert plan.fee > 0
    assert plan.change == 200_000 - 150_000 - plan.fee
    assert plan.vsize > 0
    assert "Transaction creates change." in plan.warnings


def test_build_plan_rejects_insufficient_funds(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    with pytest.raises(ValueError, match="Insufficient funds after fees"):
        planner.build_plan(
            selected_coins=selected,
            target_amount=200_000,
            fee_rate=1.0,
            funding_output_type="p2wsh",
        )


def test_build_plan_rejects_dust_change(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    with pytest.raises(
        ValueError,
        match="would be dust",
    ):
        planner.build_plan(
            selected_coins=selected,
            target_amount=199_000,
            fee_rate=1.0,
            funding_output_type="p2wsh",
        )


def test_build_plan_sweeps_all_available_funds(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    plan = planner.build_plan(
        selected_coins=selected,
        target_amount=0,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    assert plan.inputs == selected
    assert plan.amount == 200_000 - plan.fee
    assert plan.change == 0
    assert plan.amount > 0
    assert "Transaction creates change." not in plan.warnings


def test_build_plan_rejects_empty_selection() -> None:
    planner = Planner()

    with pytest.raises(
        ValueError,
        match="Insufficient funds after fees",
    ):
        planner.build_plan(
            selected_coins=[],
            target_amount=100_000,
            fee_rate=1.0,
            funding_output_type="p2wsh",
        )


def test_build_plan_with_exact_funds_has_no_change_warning(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    # Leave exactly enough for the target plus the calculated fee.
    first_plan = planner.build_plan(
        selected_coins=selected,
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    target_amount = 200_000 - first_plan.fee

    plan = planner.build_plan(
        selected_coins=selected,
        target_amount=target_amount,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    assert plan.change == 0
    assert "Transaction creates change." not in plan.warnings


def test_build_plan_includes_rationale(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    plan = planner.build_plan(
        selected_coins=selected,
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2wsh",
    )

    assert plan.rationale.startswith("Selected 2 UTXOs.")
    assert f"Estimated fee: {plan.fee} sats." in plan.rationale


def test_build_plan_accepts_p2tr_funding_output(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]

    plan = planner.build_plan(
        selected_coins=selected,
        target_amount=150_000,
        fee_rate=1.0,
        funding_output_type="p2tr",
    )

    assert plan.inputs == selected
    assert plan.amount == 150_000
    assert plan.fee > 0
    assert plan.change == 200_000 - 150_000 - plan.fee
    assert plan.vsize > 0


def test_normal_funding_fee_includes_change_output(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    planner = Planner()

    selected = classified_utxos[:2]
    fee_rate = 1.0

    normal_plan = planner.build_plan(
        selected_coins=selected,
        target_amount=150_000,
        fee_rate=fee_rate,
        funding_output_type="p2wsh",
    )

    input_types = ["p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected]

    expected_normal_vsize = estimate_vsize(
        input_types=input_types,
        output_types=["p2wsh", "p2wpkh"],
    )

    expected_sweep_vsize = estimate_vsize(
        input_types=input_types,
        output_types=["p2wsh"],
    )

    assert normal_plan.vsize == expected_normal_vsize
    assert normal_plan.vsize > expected_sweep_vsize


@pytest.mark.parametrize(
    "fee_rate",
    [0.0, -1.0, math.nan, math.inf, -math.inf],
)
def test_build_plan_rejects_invalid_fee_rate(
    classified_utxos: list[ClassifiedUTXO],
    fee_rate: float,
) -> None:
    planner = Planner()

    with pytest.raises(
        ValueError,
        match="Fee rate must be finite and positive",
    ):
        planner.build_plan(
            selected_coins=classified_utxos[:2],
            target_amount=150_000,
            fee_rate=fee_rate,
            funding_output_type="p2wsh",
        )
