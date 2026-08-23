from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import get_args

from jmwallet.wallet.models import AddressStatus

from jmlightning.models import ClassifiedUTXO


class Capability(Enum):
    """Operations a UTXO is permitted to participate in."""

    OPEN_CHANNEL = auto()
    SWAP = auto()
    SPLICE = auto()
    REMIX = auto()


@dataclass(frozen=True)
class Policy:
    """Determines what capabilities a specific AddressStatus possesses."""

    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def __init__(self, capabilities: Iterable[Capability] = ()):
        object.__setattr__(
            self,
            "capabilities",
            frozenset(capabilities),
        )

    def allows(self, capability: Capability) -> bool:
        return capability in self.capabilities


POLICIES: Mapping[AddressStatus, Policy] = {
    # ============================================================
    # Clean CoinJoin outputs
    # ============================================================
    "cj-out": Policy(
        {
            Capability.OPEN_CHANNEL,
            Capability.SPLICE,
            Capability.SWAP,
            Capability.REMIX,
        }
    ),
    # ============================================================
    # Toxic / unmixed coins
    # ============================================================
    "cj-change": Policy(
        {
            Capability.REMIX,
            Capability.SWAP,
        }
    ),
    "non-cj-change": Policy(
        {
            Capability.REMIX,
            Capability.SWAP,
        }
    ),
    "deposit": Policy(
        {
            Capability.REMIX,
            Capability.SWAP,
        }
    ),
    "reused": Policy(
        {
            Capability.REMIX,
            Capability.SWAP,
        }
    ),
    "new": Policy(
        {
            Capability.REMIX,
        }
    ),
    # ============================================================
    # Quarantined
    # ============================================================
    "reserved": Policy(),
    "bond": Policy(),
    "flagged": Policy(),
    "used-empty": Policy(),
}

#
# Startup sanity check
#

# Validate that every policy key is a JoinMarket AddressStatus.
_JM_VALID_STATUSES: set[str] = (
    {str(getattr(s, "value", s)) for s in get_args(AddressStatus)}
    if get_args(AddressStatus)
    else set()
)

if _JM_VALID_STATUSES:
    policy_keys = {str(getattr(k, "value", k)) for k in POLICIES}
    unknown = policy_keys - _JM_VALID_STATUSES
    if unknown:
        raise RuntimeError(
            f"Unknown JoinMarket AddressStatus values in POLICIES: {sorted(unknown)}"
        )


class PolicyEngine:
    """Evaluates UTXO capabilities against defined address status policies."""

    def __init__(
        self,
        policies: Mapping[AddressStatus, Policy] = POLICIES,
    ):
        self._policies = dict(policies)

    #
    # Basic lookups
    #

    def policy(self, status: AddressStatus) -> Policy:
        return self._policies[status]

    def allows(
        self,
        status: AddressStatus,
        capability: Capability,
    ) -> bool:
        return self.policy(status).allows(capability)

    #
    # Collection helpers
    #

    def filter(
        self,
        coins: Iterable[ClassifiedUTXO],
        capability: Capability,
    ) -> list[ClassifiedUTXO]:
        return [coin for coin in coins if self.allows(coin.status, capability)]

    def reject(
        self,
        coins: Iterable[ClassifiedUTXO],
        capability: Capability,
    ) -> list[ClassifiedUTXO]:
        return [coin for coin in coins if not self.allows(coin.status, capability)]

    def validate(
        self,
        coins: Iterable[ClassifiedUTXO],
        capability: Capability,
    ) -> None:
        rejected = self.reject(coins, capability)

        if not rejected:
            return

        rejected_outpoints = ", ".join(
            f"{coin.utxo.txid}:{coin.utxo.vout} (status: {coin.status})"
            for coin in rejected
        )

        raise PermissionError(
            f"The following UTXOs cannot be used for "
            f"{capability.name}: {rejected_outpoints}"
        )
