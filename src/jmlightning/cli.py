"""
Command-line interface for JoinMarket Lightning Bridge.

Configuration is loaded with the following priority (highest to lowest):

1. CLI arguments
2. Environment variables
3. Config file (~/.joinmarket-ng/config.toml)
4. Built-in defaults
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from jmcore.cli_common import (
    ResolvedMnemonic,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
)
from jmcore.cli_help import SortedTyper
from jmcore.models import NetworkType
from jmcore.process_hardening import harden_current_process
from jmcore.settings import JoinMarketSettings, ensure_config_file
from loguru import logger
from pydantic import SecretStr

from jmlightning.config import CLNConfig
from jmlightning.operations.open_channel import OpenChannelOperation

__all__ = ["app"]


app = SortedTyper(
    name="jm-lightning",
    help=("JoinMarket Lightning Bridge - Manage Lightning via strict UTXO policies"),
    no_args_is_help=True,
)


def build_cln_config(
    settings: JoinMarketSettings,
    resolved_mnemonic: ResolvedMnemonic,
    amount: int,
    mixdepth: int | None,
) -> CLNConfig:
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


@app.command()
def config_init(
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
        ),
    ] = None,
) -> None:
    """Initialise the config file with default settings."""

    from jmcore.paths import get_default_data_dir

    if data_dir is None:
        data_dir = get_default_data_dir()

    config_path = ensure_config_file(
        data_dir,
        config_file=config_file,
    )

    typer.echo(f"Config file created at: {config_path}")


@app.command()
def open_channel(
    peer_id: Annotated[
        str,
        typer.Argument(
            help="The Lightning Node ID of the peer to open a channel with",
        ),
    ],
    amount: Annotated[
        int,
        typer.Option(
            "--amount",
            "-a",
            help="Amount in sats (0 for sweep)",
        ),
    ],
    cln_socket: Annotated[
        Path,
        typer.Option(
            "--cln-socket",
            help="Path to CLN unix socket",
        ),
    ] = Path("/run/lightningd/lightning-rpc"),
    mixdepth: Annotated[
        int | None,
        typer.Option(
            "--mixdepth",
            "-m",
            help="Source mixdepth (default 0)",
        ),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
            help="JoinMarket data directory",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="JoinMarket config file path",
        ),
    ] = None,
    mnemonic_file: Annotated[
        Path | None,
        typer.Option(
            "--mnemonic-file",
            "-f",
            help="Path to mnemonic file",
        ),
    ] = None,
) -> None:
    """
    Open a CLN channel using UTXOs strictly validated by the
    capability policy engine.
    """

    settings = setup_cli(
        data_dir=data_dir,
        config_file=config_file,
    )

    resolved = resolve_mnemonic(
        settings,
        mnemonic_file=mnemonic_file,
    )

    if not resolved:
        logger.error("Could not resolve JoinMarket mnemonic.")
        raise typer.Exit(1)

    config = build_cln_config(
        settings=settings,
        resolved_mnemonic=resolved,
        amount=amount,
        mixdepth=mixdepth,
    )

    asyncio.run(
        OpenChannelOperation(
            config=config,
            cln_socket=cln_socket,
        ).execute(
            peer_id=peer_id,
        )
    )


def main() -> None:
    """Entry point."""

    harden_current_process()
    app()
