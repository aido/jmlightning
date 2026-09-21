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
import json
from pathlib import Path
from typing import Annotated

import typer
from jmcore.cli_common import (
    resolve_mnemonic,
    setup_cli,
)
from jmcore.cli_help import SortedTyper
from jmcore.process_hardening import harden_current_process
from jmcore.settings import ensure_config_file
from loguru import logger

from jmlightning.config import CLNConfig, build_cln_config
from jmlightning.operations.multi_open_channel import (
    MultiOpenChannelOperation,
    confirm_multi_open_channel,
)
from jmlightning.operations.open_channel import (
    OpenChannelOperation,
    confirm_open_channel,
)
from jmlightning.operations.peerswap import PeerSwapPrepareTxOperation, PeerSwapRuntime
from jmlightning.operations.splice import (
    SpliceOperation,
    confirm_splice_in,
)

__all__ = ["app"]


app = SortedTyper(
    name="jm-lightning",
    help=("JoinMarket Lightning Bridge - Manage Lightning via strict UTXO policies"),
    no_args_is_help=True,
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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip interactive confirmation.",
        ),
    ] = False,
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

    confirm = None if yes else confirm_open_channel

    asyncio.run(
        OpenChannelOperation(
            config=config,
            cln_socket=cln_socket,
        ).execute(
            peer_id=peer_id,
            confirm=confirm,
        )
    )


@app.command()
def multi_open_channel(
    destination: Annotated[
        list[str],
        typer.Option(
            "--destination",
            "-p",
            help="Channel destination as PEER_ID:AMOUNT_SATS (repeatable)",
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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip interactive confirmation.",
        ),
    ] = False,
) -> None:
    """Fund multiple CLN channels with one JoinMarket transaction."""
    if not destination:
        raise typer.BadParameter("At least one --destination is required")

    destinations: list[tuple[str, int]] = []
    for value in destination:
        try:
            peer_id, amount_text = value.rsplit(":", 1)
            amount = int(amount_text)
        except ValueError as exc:
            raise typer.BadParameter("Destination must be PEER_ID:AMOUNT_SATS") from exc

        if not peer_id or amount <= 0:
            raise typer.BadParameter(
                "Destination must contain a peer ID and positive amount"
            )
        destinations.append((peer_id, amount))

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
        amount=sum(amount for _, amount in destinations),
        mixdepth=mixdepth,
    )

    confirm = None if yes else confirm_multi_open_channel

    asyncio.run(
        MultiOpenChannelOperation(
            config=config,
            cln_socket=cln_socket,
        ).execute(
            destinations=destinations,
            confirm=confirm,
        )
    )


@app.command()
def splice_in(
    channel_id: Annotated[
        str,
        typer.Argument(
            help="The Lightning channel ID to splice into",
        ),
    ],
    amount: Annotated[
        int,
        typer.Option(
            "--amount",
            "-a",
            help="Splice-in amount in sats",
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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip interactive confirmation.",
        ),
    ] = False,
) -> None:
    """Splice a JoinMarket UTXO into an existing CLN channel."""

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

    confirm = None if yes else confirm_splice_in

    splice_txid = asyncio.run(
        SpliceOperation(
            config=config,
            cln_socket=cln_socket,
        ).execute(
            channel_id=channel_id,
            confirm=confirm,
        )
    )
    typer.echo(f"Splice transaction: {splice_txid}")


def _peerswap_config(
    *,
    data_dir: Path | None,
    config_file: Path | None,
    mnemonic_file: Path | None,
    amount: int,
    mixdepth: int | None,
) -> CLNConfig:
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
    return build_cln_config(
        settings=settings,
        resolved_mnemonic=resolved,
        amount=amount,
        mixdepth=mixdepth,
    )


def _run_peerswap_rpc(
    *,
    method: str,
    params: dict[str, object],
    config: CLNConfig,
    cln_socket: Path,
) -> None:
    result = PeerSwapRuntime(
        operation=PeerSwapPrepareTxOperation(
            config=config,
            cln_socket=cln_socket,
        ),
        cln_socket=cln_socket,
    ).call(method, params)
    typer.echo(json.dumps(result, sort_keys=True))


@app.command()
def peerswap_swap_in(
    short_channel_id: Annotated[str, typer.Argument()],
    amt_sat: Annotated[int, typer.Argument()],
    asset: Annotated[str, typer.Argument()],
    premium_limit_ppm: Annotated[int, typer.Argument()],
    force: Annotated[bool, typer.Option("--force")] = False,
    cln_socket: Annotated[
        Path,
        typer.Option("--cln-socket"),
    ] = Path("/run/lightningd/lightning-rpc"),
    mixdepth: Annotated[int | None, typer.Option("--mixdepth", "-m")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", envvar="JOINMARKET_DATA_DIR"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config-file", envvar="JOINMARKET_CONFIG_FILE"),
    ] = None,
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f"),
    ] = None,
) -> None:
    """Initiate the PeerSwap ``peerswap-swap-in`` RPC call."""
    config = _peerswap_config(
        data_dir=data_dir,
        config_file=config_file,
        mnemonic_file=mnemonic_file,
        amount=amt_sat,
        mixdepth=mixdepth,
    )
    _run_peerswap_rpc(
        method="peerswap-swap-in",
        params={
            "short_channel_id": short_channel_id,
            "amt_sat": amt_sat,
            "asset": asset,
            "premium_limit_ppm": premium_limit_ppm,
            "force": force,
        },
        config=config,
        cln_socket=cln_socket,
    )


@app.command()
def peerswap_swap_out(
    short_channel_id: Annotated[str, typer.Argument()],
    amt_sat: Annotated[int, typer.Argument()],
    asset: Annotated[str, typer.Argument()],
    premium_rate_limit_ppm: Annotated[int, typer.Argument()],
    force: Annotated[bool, typer.Option("--force")] = False,
    cln_socket: Annotated[
        Path,
        typer.Option("--cln-socket"),
    ] = Path("/run/lightningd/lightning-rpc"),
    mixdepth: Annotated[int | None, typer.Option("--mixdepth", "-m")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", envvar="JOINMARKET_DATA_DIR"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config-file", envvar="JOINMARKET_CONFIG_FILE"),
    ] = None,
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f"),
    ] = None,
) -> None:
    """Initiate the PeerSwap ``peerswap-swap-out`` RPC call."""
    config = _peerswap_config(
        data_dir=data_dir,
        config_file=config_file,
        mnemonic_file=mnemonic_file,
        amount=amt_sat,
        mixdepth=mixdepth,
    )
    _run_peerswap_rpc(
        method="peerswap-swap-out",
        params={
            "short_channel_id": short_channel_id,
            "amt_sat": amt_sat,
            "asset": asset,
            "premium_rate_limit_ppm": premium_rate_limit_ppm,
            "force": force,
        },
        config=config,
        cln_socket=cln_socket,
    )


def main() -> None:
    """Entry point."""

    harden_current_process()
    app()
