"""
Configuration for JoinMarket Lightning module.
"""

from __future__ import annotations

from jmcore.cli_common import ResolvedMnemonic, resolve_backend_settings
from jmcore.config import WalletConfig
from jmcore.models import NetworkType
from jmcore.settings import JoinMarketSettings
from pydantic import Field, SecretStr, model_validator

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


def build_cln_config(
    settings: JoinMarketSettings,
    resolved_mnemonic: ResolvedMnemonic,
    amount: int = 0,
    mixdepth: int | None = None,
) -> CLNConfig:
    """Build the JoinMarket Lightning configuration from common settings."""
    wallet = settings.wallet
    backend = resolve_backend_settings(
        settings,
        network=settings.network_config.network,
        bitcoin_network=settings.network_config.bitcoin_network,
        data_dir=settings.data_dir,
    )

    return CLNConfig(
        mnemonic=SecretStr(resolved_mnemonic.mnemonic),
        passphrase=SecretStr(resolved_mnemonic.bip39_passphrase),
        creation_height=resolved_mnemonic.creation_height,
        network=NetworkType(backend.network),
        bitcoin_network=(
            NetworkType(backend.bitcoin_network)
            if backend.bitcoin_network is not None
            else None
        ),
        data_dir=backend.data_dir,
        backend_type=backend.backend_type,
        backend_config={
            "rpc_url": backend.rpc_url,
            "rpc_user": backend.rpc_user,
            "rpc_password": backend.rpc_password,
            "rpc_cookie_file": settings.bitcoin.rpc_cookie_file,
            "neutrino_url": backend.neutrino_url,
            "scan_start_height": backend.scan_start_height,
            "add_peers": backend.neutrino_add_peers,
            "tls_cert_path": backend.neutrino_tls_cert,
            "auth_token": backend.neutrino_auth_token,
            "include_mempool": settings.bitcoin.neutrino_include_mempool,
            "fee_estimate_url": backend.fee_estimate_url,
            "fee_estimate_proxy": backend.fee_estimate_proxy,
        },
        mixdepth_count=wallet.mixdepth_count,
        gap_limit=wallet.gap_limit,
        scan_range=wallet.scan_range,
        max_sats_freeze_reuse=wallet.max_sats_freeze_reuse,
        reconstruct_history=wallet.reconstruct_history,
        amount=amount,
        mixdepth=0 if mixdepth is None else mixdepth,
    )
