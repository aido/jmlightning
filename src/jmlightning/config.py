"""
Configuration for JoinMarket Lightning module.
"""

from __future__ import annotations

from jmcore.config import WalletConfig
from pydantic import Field, model_validator

from jmlightning.lightning.backend import FeePriority


class CLNConfig(WalletConfig):
    """
    Configuration for cln bridge.

    Inherits base wallet configuration from jmcore.config.WalletConfig
    and adds cln-specific settings for Lightning, submarine swap execution,
    and splicing.
    """

    # CLN settings
    amount: int = Field(default=0, ge=0, description="Amount in sats (0 = sweep)")
    mixdepth: int = Field(default=0, ge=0, description="Source mixdepth")
    announce: bool = Field(
        default=False, description="Announce the channel to the Lightning network"
    )
    fee_priority: FeePriority = Field(
        default=FeePriority.NORMAL, description="Priority level for transaction fees"
    )

    @model_validator(mode="after")
    def set_bitcoin_network_default(self) -> CLNConfig:
        """If bitcoin_network is not set, default to the protocol network."""
        if self.bitcoin_network is None:
            object.__setattr__(self, "bitcoin_network", self.network)
        return self
