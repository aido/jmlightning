from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from jmcore.bitcoin import estimate_vsize
from jmcore.constants import DUST_THRESHOLD

from jmlightning.models import ClassifiedUTXO


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    inputs: list[ClassifiedUTXO]
    amount: int
    fee: int
    vsize: int
    change: int
    warnings: list[str]
    rationale: str


class Planner:
    """Builds a transaction execution plan from a validated set of UTXOs."""

    def build_plan(
        self,
        selected_coins: list[ClassifiedUTXO],
        target_amount: int,
        fee_rate: float,
        funding_output_type: str,
    ) -> ExecutionPlan:
        """
        Build a funding plan.

        Assumes the caller has already validated that the selected coins
        are permitted for the intended capability.
        """
        accumulated = sum(coin.utxo.value for coin in selected_coins)

        sweep = target_amount == 0

        input_types = [
            "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected_coins
        ]

        output_types = [funding_output_type]

        if not sweep:
            # Assume a single P2WPKH change output.
            output_types.append("p2wpkh")

        vsize = estimate_vsize(
            input_types=input_types,
            output_types=output_types,
        )

        fee = ceil(vsize * fee_rate)

        if sweep:
            amount = accumulated - fee
            change = 0

            if amount <= 0:
                raise ValueError("Insufficient funds after fees.")
        else:
            amount = target_amount
            change = accumulated - amount - fee

            if change < 0:
                raise ValueError("Insufficient funds after fees.")

        warnings: list[str] = []

        if 0 < change < DUST_THRESHOLD:
            raise ValueError(f"Change output ({change} sats) would be dust.")

        if change > 0:
            warnings.append("Transaction creates change.")

        return ExecutionPlan(
            inputs=selected_coins,
            amount=amount,
            fee=fee,
            vsize=vsize,
            change=change,
            warnings=warnings,
            rationale=(
                f"Selected {len(selected_coins)} UTXOs. Estimated fee: {fee} sats."
            ),
        )
