from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, isfinite

from jmcore.bitcoin import estimate_vsize
from jmcore.constants import DUST_THRESHOLD

from jmlightning.models import ClassifiedUTXO


@dataclass(frozen=True, slots=True)
class FundingOutput:
    """One channel funding output in an execution plan."""

    amount: int
    output_type: str


@dataclass(slots=True)
class ExecutionPlan:
    inputs: list[ClassifiedUTXO]
    amount: int
    fee: int
    vsize: int
    change: int
    warnings: list[str]
    rationale: str
    funding_outputs: list[FundingOutput] = field(default_factory=list)


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
        return self.build_multi_plan(
            selected_coins=selected_coins,
            target_amounts=[target_amount],
            fee_rate=fee_rate,
            funding_output_types=[funding_output_type],
        )

    def build_multi_plan(
        self,
        selected_coins: list[ClassifiedUTXO],
        target_amounts: list[int],
        fee_rate: float,
        funding_output_types: list[str],
    ) -> ExecutionPlan:
        """Build a funding plan with one or more channel outputs.

        Assumes the caller has already validated that the selected coins
        are permitted for the intended capability.
        """
        if not isfinite(fee_rate) or fee_rate <= 0:
            raise ValueError("Fee rate must be finite and positive.")

        if not target_amounts:
            raise ValueError("At least one funding output is required.")

        if len(target_amounts) != len(funding_output_types):
            raise ValueError(
                "Funding amounts and output types must have the same length."
            )

        sweep = len(target_amounts) == 1 and target_amounts[0] == 0
        if any(amount < 0 for amount in target_amounts) or (
            any(amount == 0 for amount in target_amounts) and not sweep
        ):
            raise ValueError("Funding output amounts must be positive.")

        accumulated = sum(coin.utxo.value for coin in selected_coins)
        target_amount = sum(target_amounts)

        input_types = [
            "p2wsh" if coin.utxo.is_p2wsh else "p2wpkh" for coin in selected_coins
        ]

        output_types = list(funding_output_types)

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
            funding_outputs=[
                FundingOutput(amount=amount, output_type=output_type)
                for amount, output_type in zip(
                    target_amounts,
                    funding_output_types,
                    strict=True,
                )
            ],
        )
