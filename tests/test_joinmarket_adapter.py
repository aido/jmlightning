from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.models import ClassifiedUTXO


def test_get_utxos_returns_confirmed_classified_coins(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    confirmed = classified_utxos[0].utxo
    info = SimpleNamespace(
        base_status="cj-out",
        status="cj-out",
        utxos=[confirmed],
    )

    with patch.object(
        adapter,
        "_address_infos",
        side_effect=[
            [info],
            [],
        ],
    ):
        result = adapter.get_utxos(mixdepth=0)

    assert len(result) == 1
    assert result[0].utxo == confirmed
    assert result[0].status == "cj-out"


def test_get_utxos_passes_mixdepth_to_address_lookup(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    confirmed = classified_utxos[0].utxo

    info = SimpleNamespace(
        base_status="cj-out",
        status="cj-out",
        utxos=[confirmed],
    )

    address_infos = Mock(return_value=[info])

    with patch.object(
        adapter,
        "_address_infos",
        address_infos,
    ):
        result = adapter.get_utxos(mixdepth=3)

    assert address_infos.call_count == 2
    assert address_infos.call_args_list == [
        ((3, 0), {}),
        ((3, 1), {}),
    ]

    assert len(result) == 2
    assert result[0].utxo == confirmed


def test_get_utxos_returns_empty_list_when_no_addresses_have_utxos() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    with patch.object(
        adapter,
        "_address_infos",
        return_value=[],
    ):
        result = adapter.get_utxos(mixdepth=0)

    assert result == []


def test_get_utxos_returns_all_utxos_from_address_info(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    first = classified_utxos[0].utxo
    second = classified_utxos[1].utxo

    info = SimpleNamespace(
        base_status="cj-out",
        status="cj-out",
        utxos=[first, second],
    )

    with patch.object(
        adapter,
        "_address_infos",
        side_effect=[
            [info],
            [],
        ],
    ):
        result = adapter.get_utxos(mixdepth=0)

    assert len(result) == 2
    assert result[0].utxo == first
    assert result[1].utxo == second
    assert result[0].status == "cj-out"
    assert result[1].status == "cj-out"


def test_get_utxos_preserves_address_status(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    confirmed = classified_utxos[0].utxo

    info = SimpleNamespace(
        base_status="cj-change",
        status="cj-change",
        utxos=[confirmed],
    )

    with patch.object(
        adapter,
        "_address_infos",
        side_effect=[
            [info],
            [],
        ],
    ):
        result = adapter.get_utxos(mixdepth=0)

    assert len(result) == 1
    assert result[0].utxo == confirmed
    assert result[0].status == "cj-change"


def test_lock_freezes_utxo_and_records_lock(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    coin = classified_utxos[0]

    adapter.lock(coin)

    adapter.wallet.freeze_utxo.assert_called_once_with(
        f"{coin.utxo.txid}:{coin.utxo.vout}",
    )

    assert (coin.utxo.txid, coin.utxo.vout) in adapter._locked_utxos


def test_lock_rejects_already_locked_utxo(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    coin = classified_utxos[0]

    adapter.lock(coin)

    with pytest.raises(
        ValueError,
        match="is already locked",
    ):
        adapter.lock(coin)

    adapter.wallet.freeze_utxo.assert_called_once_with(
        f"{coin.utxo.txid}:{coin.utxo.vout}",
    )


def test_unlock_unfreezes_utxo_and_removes_lock(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    coin = classified_utxos[0]

    adapter.lock(coin)
    adapter.unlock(coin)

    adapter.wallet.unfreeze_utxo.assert_called_once_with(
        f"{coin.utxo.txid}:{coin.utxo.vout}",
    )

    assert (coin.utxo.txid, coin.utxo.vout) not in adapter._locked_utxos


def test_unlock_untracked_utxo_still_unfreezes(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    coin = classified_utxos[0]

    adapter.unlock(coin)

    adapter.wallet.unfreeze_utxo.assert_called_once_with(
        f"{coin.utxo.txid}:{coin.utxo.vout}",
    )

    assert (coin.utxo.txid, coin.utxo.vout) not in adapter._locked_utxos


def test_lock_rejects_disconnected_adapter(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())

    with pytest.raises(
        RuntimeError,
        match="JoinMarketAdapter is not connected",
    ):
        adapter.lock(classified_utxos[0])


def test_unlock_rejects_disconnected_adapter(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())

    with pytest.raises(
        RuntimeError,
        match="JoinMarketAdapter is not connected",
    ):
        adapter.unlock(classified_utxos[0])


def test_get_change_address_rejects_disconnected_adapter() -> None:
    adapter = JoinMarketAdapter(config=Mock())

    with pytest.raises(
        RuntimeError,
        match="JoinMarketAdapter is not connected",
    ):
        adapter.get_change_address(mixdepth=0)


@pytest.mark.anyio
async def test_connect_creates_and_syncs_wallet() -> None:
    config = Mock()
    config.bitcoin_network = Mock(value="regtest")
    config.network = Mock(value="mainnet")
    config.backend_type = "descriptor_wallet"
    config.mnemonic.get_secret_value.return_value = "test mnemonic"
    config.passphrase.get_secret_value.return_value = ""
    config.mixdepth_count = 5
    config.gap_limit = 6
    config.scan_range = 20
    config.data_dir = "/tmp/jmlightning-test"
    config.max_sats_freeze_reuse = 0
    config.reconstruct_history = False
    config.backend_config = {
        "rpc_url": "http://127.0.0.1:18443",
        "rpc_user": "test",
        "rpc_password": "test",
    }
    config.creation_height = None

    adapter = JoinMarketAdapter(config=config)

    backend = Mock()
    backend.get_block_height = AsyncMock()

    wallet = Mock()
    wallet.sync_with_registered_bonds = AsyncMock()

    with (
        patch.object(
            adapter,
            "_create_backend",
            return_value=backend,
        ) as create_backend,
        patch(
            "jmlightning.adapters.joinmarket.WalletService",
            return_value=wallet,
        ),
    ):
        await adapter.connect()

    create_backend.assert_called_once_with(
        config.bitcoin_network,
    )

    backend.get_block_height.assert_awaited_once_with()
    wallet.sync_with_registered_bonds.assert_awaited_once_with()
    assert adapter.wallet is wallet


@pytest.mark.anyio
async def test_close_closes_wallet_and_clears_adapter() -> None:
    adapter = JoinMarketAdapter(config=Mock())

    wallet = Mock()
    wallet.close = AsyncMock()
    adapter.wallet = wallet

    await adapter.close()

    wallet.close.assert_awaited_once()
    assert adapter.wallet is None


@pytest.mark.anyio
async def test_close_is_safe_when_already_disconnected() -> None:
    adapter = JoinMarketAdapter(config=Mock())

    await adapter.close()

    assert adapter.wallet is None


def test_create_backend_selects_descriptor_wallet() -> None:
    config = Mock()
    config.backend_type = "descriptor_wallet"
    config.backend_config = {
        "rpc_url": "http://127.0.0.1:18443",
        "rpc_user": "test",
        "rpc_password": "test",
    }
    config.mnemonic.get_secret_value.return_value = "test mnemonic"
    config.passphrase.get_secret_value.return_value = ""
    config.creation_height = None

    config_network = Mock(value="regtest")
    adapter = JoinMarketAdapter(config=config)

    backend = Mock()

    with (
        patch(
            "jmwallet.backends.descriptor_wallet.get_mnemonic_fingerprint",
            return_value="deadbeef",
        ) as fingerprint,
        patch(
            "jmwallet.backends.descriptor_wallet.generate_wallet_name",
            return_value="jm-test-wallet",
        ) as wallet_name,
        patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=backend,
        ) as descriptor_backend,
    ):
        result = adapter._create_backend(config_network)

    assert result is backend
    fingerprint.assert_called_once_with(
        "test mnemonic",
        "",
    )
    wallet_name.assert_called_once_with(
        "deadbeef",
        "regtest",
    )
    descriptor_backend.assert_called_once_with(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="test",
        rpc_password="test",
        wallet_name="jm-test-wallet",
    )


def test_create_backend_selects_neutrino() -> None:
    config = Mock()
    config.backend_type = "neutrino"
    config.backend_config = {
        "neutrino_url": "http://127.0.0.1:8334",
        "scan_start_height": 100,
        "add_peers": ["127.0.0.1:9735"],
        "tls_cert_path": "/tmp/neutrino.cert",
        "auth_token": "token",
        "include_mempool": False,
        "fee_estimate_url": "http://127.0.0.1:8080",
        "fee_estimate_proxy": "http://127.0.0.1:9050",
    }
    config.creation_height = None

    config_network = Mock(value="regtest")
    adapter = JoinMarketAdapter(config=config)

    backend = Mock()

    with patch(
        "jmwallet.backends.neutrino.NeutrinoBackend",
        return_value=backend,
    ) as neutrino_backend:
        result = adapter._create_backend(config_network)

    assert result is backend
    neutrino_backend.assert_called_once_with(
        neutrino_url="http://127.0.0.1:8334",
        network="regtest",
        scan_start_height=100,
        add_peers=["127.0.0.1:9735"],
        tls_cert_path="/tmp/neutrino.cert",
        auth_token="token",
        include_mempool=False,
        fee_estimate_url="http://127.0.0.1:8080",
        fee_estimate_proxy="http://127.0.0.1:9050",
    )


def test_create_backend_rejects_unknown_backend_type() -> None:
    config = Mock()
    config.backend_type = "unknown"
    config.backend_config = {}
    config.creation_height = None

    adapter = JoinMarketAdapter(config=config)
    config_network = Mock(value="regtest")

    with pytest.raises(
        ValueError,
        match="Unknown backend type: unknown",
    ):
        adapter._create_backend(config_network)


def test_create_backend_sets_creation_height() -> None:
    config = Mock()
    config.backend_type = "descriptor_wallet"
    config.backend_config = {
        "rpc_url": "http://127.0.0.1:18443",
        "rpc_user": "test",
        "rpc_password": "test",
    }
    config.mnemonic.get_secret_value.return_value = "test mnemonic"
    config.passphrase.get_secret_value.return_value = ""
    config.creation_height = 123

    network = Mock(value="regtest")
    adapter = JoinMarketAdapter(config=config)

    backend = Mock()

    with (
        patch(
            "jmwallet.backends.descriptor_wallet.get_mnemonic_fingerprint",
            return_value="deadbeef",
        ),
        patch(
            "jmwallet.backends.descriptor_wallet.generate_wallet_name",
            return_value="jm-test-wallet",
        ),
        patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=backend,
        ),
    ):
        result = adapter._create_backend(network)

    assert result is backend
    backend.set_wallet_creation_height.assert_called_once_with(123)


def test_create_backend_does_not_set_creation_height_when_unconfigured() -> None:
    config = Mock()
    config.backend_type = "descriptor_wallet"
    config.backend_config = {
        "rpc_url": "http://127.0.0.1:18443",
        "rpc_user": "test",
        "rpc_password": "test",
    }
    config.mnemonic.get_secret_value.return_value = "test mnemonic"
    config.passphrase.get_secret_value.return_value = ""
    config.creation_height = None

    network = Mock(value="regtest")
    adapter = JoinMarketAdapter(config=config)

    backend = Mock()

    with (
        patch(
            "jmwallet.backends.descriptor_wallet.get_mnemonic_fingerprint",
            return_value="deadbeef",
        ),
        patch(
            "jmwallet.backends.descriptor_wallet.generate_wallet_name",
            return_value="jm-test-wallet",
        ),
        patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=backend,
        ),
    ):
        result = adapter._create_backend(network)

    assert result is backend
    backend.set_wallet_creation_height.assert_not_called()
