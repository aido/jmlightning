from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import jmlightning.cli as cli
import jmlightning.config as config
from jmlightning.operations.open_channel import confirm_open_channel
from jmlightning.operations.splice import confirm_splice_in

runner = CliRunner()


def _settings() -> Any:
    return SimpleNamespace(
        wallet=SimpleNamespace(
            mixdepth_count=5,
            gap_limit=6,
            scan_range=100,
            max_sats_freeze_reuse=100_000,
            reconstruct_history=False,
        ),
        network_config=SimpleNamespace(
            network="regtest",
            bitcoin_network="regtest",
        ),
        data_dir=Path("/tmp/joinmarket"),
        bitcoin=SimpleNamespace(
            rpc_cookie_file=Path("/tmp/.cookie"),
            neutrino_include_mempool=True,
        ),
    )


def _backend() -> Any:
    return SimpleNamespace(
        network="regtest",
        bitcoin_network="regtest",
        data_dir=Path("/tmp/joinmarket"),
        backend_type="bitcoind",
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="password",
        neutrino_url=None,
        scan_start_height=None,
        neutrino_add_peers=False,
        neutrino_tls_cert=None,
        neutrino_auth_token=None,
        fee_estimate_url=None,
        fee_estimate_proxy=None,
    )


def _resolved_mnemonic() -> Any:
    return SimpleNamespace(
        mnemonic=(
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        ),
        bip39_passphrase="",
        creation_height=100,
    )


def test_config_init_passes_data_dir_and_config_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = Path("/tmp/test-jm")
    config_file = Path("/tmp/test-config.toml")
    config_path = data_dir / "config.toml"
    calls: dict[str, Any] = {}

    def fake_ensure_config_file(
        passed_data_dir: Path,
        *,
        config_file: Path | None,
    ) -> Path:
        calls["data_dir"] = passed_data_dir
        calls["config_file"] = config_file
        return config_path

    monkeypatch.setattr(
        cli,
        "ensure_config_file",
        fake_ensure_config_file,
    )

    result = runner.invoke(
        cli.app,
        [
            "config-init",
            "--data-dir",
            str(data_dir),
            "--config-file",
            str(config_file),
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == f"Config file created at: {config_path}\n"
    assert calls == {
        "data_dir": data_dir,
        "config_file": config_file,
    }


def test_build_cln_config_resolves_backend_and_maps_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    resolved = _resolved_mnemonic()
    backend = _backend()

    monkeypatch.setattr(
        config,
        "resolve_backend_settings",
        lambda *args, **kwargs: backend,
    )

    result = config.build_cln_config(
        settings=settings,
        resolved_mnemonic=resolved,
        amount=250_000,
        mixdepth=None,
    )

    assert result.mnemonic.get_secret_value() == resolved.mnemonic
    assert result.passphrase.get_secret_value() == resolved.bip39_passphrase
    assert result.creation_height == resolved.creation_height
    assert result.amount == 250_000
    assert result.mixdepth == 0
    assert result.network.value == "regtest"
    assert result.bitcoin_network is not None
    assert result.bitcoin_network.value == "regtest"
    assert result.data_dir == backend.data_dir
    assert result.backend_type == "bitcoind"
    assert result.mixdepth_count == settings.wallet.mixdepth_count
    assert result.gap_limit == settings.wallet.gap_limit
    assert result.scan_range == settings.wallet.scan_range
    assert result.backend_config == {
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
    }


def test_build_cln_config_defaults_bitcoin_network_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    backend = _backend()
    backend.bitcoin_network = None
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        config,
        "resolve_backend_settings",
        lambda *args, **kwargs: backend,
    )
    monkeypatch.setattr(
        config,
        "CLNConfig",
        lambda **kwargs: captured.update(kwargs) or kwargs,
    )

    config.build_cln_config(
        settings=settings,
        resolved_mnemonic=_resolved_mnemonic(),
        amount=0,
        mixdepth=3,
    )

    assert captured["bitcoin_network"] is None
    assert captured["mixdepth"] == 3


def test_open_channel_exits_when_mnemonic_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.open_channel(
            peer_id="02" + "11" * 32,
            amount=100_000,
        )

    assert exc_info.value.exit_code == 1


def test_open_channel_runs_operation_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    resolved = _resolved_mnemonic()
    config = object()
    operation_calls: dict[str, Any] = {}

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: resolved,
    )
    monkeypatch.setattr(
        cli,
        "build_cln_config",
        lambda **kwargs: config,
    )

    class FakeOperation:
        def __init__(
            self,
            *,
            config: Any,
            cln_socket: Path,
        ) -> None:
            operation_calls["config"] = config
            operation_calls["cln_socket"] = cln_socket

        def execute(
            self,
            *,
            peer_id: str,
            confirm: Any,
        ) -> None:
            operation_calls["peer_id"] = peer_id
            operation_calls["confirm"] = confirm

    monkeypatch.setattr(
        cli,
        "OpenChannelOperation",
        FakeOperation,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda awaitable: _close_awaitable(awaitable),
    )

    cli.open_channel(
        peer_id="02" + "11" * 32,
        amount=100_000,
        cln_socket=Path("/tmp/lightning-rpc"),
        mixdepth=2,
        yes=True,
    )

    assert operation_calls == {
        "config": config,
        "cln_socket": Path("/tmp/lightning-rpc"),
        "peer_id": "02" + "11" * 32,
        "confirm": None,
    }


def test_open_channel_passes_confirmation_callback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    resolved = _resolved_mnemonic()
    confirms: list[Any] = []

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: resolved,
    )
    monkeypatch.setattr(
        cli,
        "build_cln_config",
        lambda **kwargs: object(),
    )

    class FakeOperation:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def execute(
            self,
            *,
            peer_id: str,
            confirm: Any,
        ) -> None:
            confirms.append(confirm)

    monkeypatch.setattr(
        cli,
        "OpenChannelOperation",
        FakeOperation,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda awaitable: _close_awaitable(awaitable),
    )

    cli.open_channel(
        peer_id="peer",
        amount=100_000,
    )

    assert confirms == [confirm_open_channel]


def test_splice_in_runs_operation_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    resolved = _resolved_mnemonic()
    config = object()
    operation_calls: dict[str, Any] = {}

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: resolved,
    )
    monkeypatch.setattr(
        cli,
        "build_cln_config",
        lambda **kwargs: config,
    )

    class FakeOperation:
        def __init__(
            self,
            *,
            config: Any,
            cln_socket: Path,
        ) -> None:
            operation_calls["config"] = config
            operation_calls["cln_socket"] = cln_socket

        def execute(
            self,
            *,
            channel_id: str,
            confirm: Any,
        ) -> None:
            operation_calls["channel_id"] = channel_id
            operation_calls["confirm"] = confirm

    monkeypatch.setattr(
        cli,
        "SpliceOperation",
        FakeOperation,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda awaitable: _close_awaitable(awaitable),
    )

    cli.splice_in(
        channel_id="22" * 32,
        amount=250_000,
        cln_socket=Path("/tmp/lightning-rpc"),
        mixdepth=2,
        yes=True,
    )

    assert operation_calls == {
        "config": config,
        "cln_socket": Path("/tmp/lightning-rpc"),
        "channel_id": "22" * 32,
        "confirm": None,
    }


def test_splice_in_exits_when_mnemonic_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.splice_in(
            channel_id="11" * 32,
            amount=100_000,
        )

    assert exc_info.value.exit_code == 1


def test_splice_in_passes_confirmation_callback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    resolved = _resolved_mnemonic()
    confirms: list[Any] = []

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: resolved,
    )
    monkeypatch.setattr(
        cli,
        "build_cln_config",
        lambda **kwargs: object(),
    )

    class FakeOperation:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def execute(
            self,
            *,
            channel_id: str,
            confirm: Any,
        ) -> None:
            confirms.append(confirm)

    monkeypatch.setattr(
        cli,
        "SpliceOperation",
        FakeOperation,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda awaitable: _close_awaitable(awaitable),
    )

    cli.splice_in(
        channel_id="22" * 32,
        amount=250_000,
    )

    assert confirms == [confirm_splice_in]


def test_splice_in_skips_confirmation_with_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    resolved = _resolved_mnemonic()
    confirms: list[Any] = []

    monkeypatch.setattr(
        cli,
        "setup_cli",
        lambda **kwargs: settings,
    )
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: resolved,
    )
    monkeypatch.setattr(
        cli,
        "build_cln_config",
        lambda **kwargs: object(),
    )

    class FakeOperation:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def execute(
            self,
            *,
            channel_id: str,
            confirm: Any,
        ) -> None:
            confirms.append(confirm)

    monkeypatch.setattr(
        cli,
        "SpliceOperation",
        FakeOperation,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda awaitable: _close_awaitable(awaitable),
    )

    cli.splice_in(
        channel_id="22" * 32,
        amount=250_000,
        yes=True,
    )

    assert confirms == [None]


def test_peerswap_swap_in_calls_matching_cln_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli,
        "_peerswap_config",
        lambda **kwargs: captured.setdefault("config", object()),
    )

    def fake_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "_run_peerswap_rpc", fake_run)

    cli.peerswap_swap_in(
        short_channel_id="1x2x3",
        amt_sat=250_000,
        asset="btc",
        premium_limit_ppm=25,
        force=True,
        cln_socket=Path("/tmp/lightning-rpc"),
    )

    assert captured["method"] == "peerswap-swap-in"
    assert captured["params"] == {
        "short_channel_id": "1x2x3",
        "amt_sat": 250_000,
        "asset": "btc",
        "premium_limit_ppm": 25,
        "force": True,
    }
    assert captured["cln_socket"] == Path("/tmp/lightning-rpc")


def test_peerswap_swap_out_calls_matching_cln_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli,
        "_peerswap_config",
        lambda **kwargs: captured.setdefault("config", object()),
    )

    def fake_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "_run_peerswap_rpc", fake_run)

    cli.peerswap_swap_out(
        short_channel_id="1x2x3",
        amt_sat=250_000,
        asset="btc",
        premium_rate_limit_ppm=25,
        force=False,
        cln_socket=Path("/tmp/lightning-rpc"),
    )

    assert captured["method"] == "peerswap-swap-out"
    assert captured["params"] == {
        "short_channel_id": "1x2x3",
        "amt_sat": 250_000,
        "asset": "btc",
        "premium_rate_limit_ppm": 25,
        "force": False,
    }
    assert captured["cln_socket"] == Path("/tmp/lightning-rpc")


def _close_awaitable(awaitable: Any) -> None:
    if asyncio.iscoroutine(awaitable):
        awaitable.close()


def test_main_hardens_process_and_starts_typer_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        cli,
        "harden_current_process",
        lambda: calls.append("harden"),
    )
    monkeypatch.setattr(
        cli,
        "app",
        lambda: calls.append("app"),
    )

    cli.main()

    assert calls == ["harden", "app"]
