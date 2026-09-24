import asyncio
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jmwallet.wallet.service import WalletService

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.models import ClassifiedUTXO


@pytest.mark.anyio
async def test_get_mempool_min_fee_returns_none_when_backend_has_no_getter() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend = SimpleNamespace()

    assert await adapter.get_mempool_min_fee() is None


@pytest.mark.anyio
@pytest.mark.parametrize("value", [True, 0, -0.1, "0.1"])
async def test_get_mempool_min_fee_rejects_invalid_backend_values(
    value: object,
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_mempool_min_fee = AsyncMock(return_value=value)

    with pytest.raises(RuntimeError, match="mempool fee rate"):
        await adapter.get_mempool_min_fee()


def test_get_utxos_returns_confirmed_classified_coins(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.get_locked_input_outpoints.return_value = set()

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
    adapter.wallet.get_locked_input_outpoints.return_value = set()

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
    adapter.wallet.get_locked_input_outpoints.return_value = set()

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
    adapter.wallet.get_locked_input_outpoints.return_value = set()

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
    adapter.wallet.get_locked_input_outpoints.return_value = set()

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


def test_lock_reserves_utxo_without_persistent_freeze(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True

    coin = classified_utxos[0]

    adapter.lock(coin)

    adapter.wallet.reserve_coinjoin_inputs.assert_called_once()
    args = adapter.wallet.reserve_coinjoin_inputs.call_args.args
    assert args[0] == {(coin.utxo.txid, coin.utxo.vout)}
    assert adapter.wallet.reserve_coinjoin_inputs.call_args.kwargs["owner"]
    assert (coin.utxo.txid, coin.utxo.vout) in adapter._locked_utxos
    assert (coin.utxo.txid, coin.utxo.vout) in adapter._lock_owners
    adapter.wallet.freeze_utxo.assert_not_called()


def test_lock_uses_atomic_owned_metadata_reservation(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True

    coin = classified_utxos[0]

    adapter.lock(coin)

    adapter.wallet.reserve_coinjoin_inputs.assert_called_once()
    assert adapter.wallet.reserve_coinjoin_inputs.call_args.kwargs["ttl"] == 30 * 60
    owner = adapter.wallet.reserve_coinjoin_inputs.call_args.kwargs["owner"]
    assert owner
    assert adapter._lock_owners[(coin.utxo.txid, coin.utxo.vout)] == owner


def test_lock_rejects_utxo_already_reserved_by_another_process(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = False

    coin = classified_utxos[0]

    with pytest.raises(ValueError, match="is already locked"):
        adapter.lock(coin)

    adapter.wallet.freeze_utxo.assert_not_called()
    adapter.wallet.release_coinjoin_inputs.assert_not_called()
    assert (coin.utxo.txid, coin.utxo.vout) not in adapter._locked_utxos
    assert (coin.utxo.txid, coin.utxo.vout) not in adapter._lock_owners


def test_lock_rejects_already_locked_utxo(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True

    coin = classified_utxos[0]

    adapter.lock(coin)

    with pytest.raises(ValueError, match="is already locked"):
        adapter.lock(coin)

    adapter.wallet.reserve_coinjoin_inputs.assert_called_once()


def test_renew_extends_owned_reservation(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True
    adapter.wallet.renew_coinjoin_inputs.return_value = True

    coin = classified_utxos[0]
    outpoint = (coin.utxo.txid, coin.utxo.vout)

    adapter.lock(coin)
    owner = adapter._lock_owners[outpoint]
    adapter.renew(coin)

    adapter.wallet.renew_coinjoin_inputs.assert_called_once_with(
        {outpoint},
        owner=owner,
        ttl=30 * 60,
    )


def test_renew_rejects_missing_local_owner(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    with pytest.raises(RuntimeError, match="no local.*owner"):
        adapter.renew(classified_utxos[0])

    adapter.wallet.renew_coinjoin_inputs.assert_not_called()


def test_renew_detects_expired_or_replaced_reservation(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True
    adapter.wallet.renew_coinjoin_inputs.return_value = False

    coin = classified_utxos[0]
    outpoint = (coin.utxo.txid, coin.utxo.vout)

    adapter.lock(coin)
    owner = adapter._lock_owners[outpoint]

    with pytest.raises(RuntimeError, match="expired or is no longer owned"):
        adapter.renew(coin)

    # Keep the owner generation after failed renewal so cleanup remains
    # compare-and-release safe and can be retried without losing ownership
    # information.
    assert adapter._lock_owners[outpoint] == owner
    assert outpoint in adapter._locked_utxos


def test_renew_locks_renews_each_owner_generation(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True
    adapter.wallet.renew_coinjoin_inputs.return_value = True

    coins = classified_utxos[:2]
    adapter.lock(coins[0])
    adapter.lock(coins[1])
    adapter.renew_locks(coins)

    assert adapter.wallet.renew_coinjoin_inputs.call_count == 2
    for coin in coins:
        outpoint = (coin.utxo.txid, coin.utxo.vout)
        assert adapter._lock_owners[outpoint]


def test_lock_renewal_runs_outside_event_loop(
    classified_utxos: list[ClassifiedUTXO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True
    adapter.wallet.renew_coinjoin_inputs.return_value = True

    monkeypatch.setattr(
        "jmlightning.adapters.joinmarket.LOCK_RENEWAL_INTERVAL_SECONDS",
        0.01,
    )

    adapter.lock(classified_utxos[0])
    adapter.start_lock_renewal()

    # Deliberately block the calling thread. An asyncio task would not get a
    # chance to run here, but the renewal worker must still execute.
    time.sleep(0.05)

    asyncio.run(adapter.stop_lock_renewal())
    assert adapter.wallet.renew_coinjoin_inputs.call_count >= 2


def test_unlock_releases_owned_reservation_without_unfreezing(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True

    coin = classified_utxos[0]
    outpoint = (coin.utxo.txid, coin.utxo.vout)

    adapter.lock(coin)
    owner = adapter._lock_owners[outpoint]
    adapter.unlock(coin)

    adapter.wallet.release_coinjoin_inputs.assert_called_once_with(
        {outpoint},
        owner=owner,
    )
    adapter.wallet.unfreeze_utxo.assert_not_called()
    assert outpoint not in adapter._locked_utxos
    assert outpoint not in adapter._lock_owners


def test_unlock_retains_owner_when_release_fails(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.reserve_coinjoin_inputs.return_value = True

    coin = classified_utxos[0]
    outpoint = (coin.utxo.txid, coin.utxo.vout)

    adapter.lock(coin)
    owner = adapter._lock_owners[outpoint]
    adapter.wallet.release_coinjoin_inputs.side_effect = RuntimeError(
        "metadata unavailable"
    )

    with pytest.raises(RuntimeError, match="metadata unavailable"):
        adapter.unlock(coin)

    assert adapter._lock_owners[outpoint] == owner
    assert outpoint in adapter._locked_utxos
    adapter.wallet.unfreeze_utxo.assert_not_called()


def test_unlock_untracked_utxo_does_not_unfreeze(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    coin = classified_utxos[0]

    adapter.unlock(coin)

    adapter.wallet.unfreeze_utxo.assert_not_called()
    adapter.wallet.release_coinjoin_inputs.assert_not_called()
    assert (coin.utxo.txid, coin.utxo.vout) not in adapter._locked_utxos
    assert (coin.utxo.txid, coin.utxo.vout) not in adapter._lock_owners


def test_get_utxos_excludes_cross_process_joinmarket_locks(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    locked = classified_utxos[0].utxo
    available = classified_utxos[1].utxo
    adapter.wallet.get_locked_input_outpoints.return_value = {
        (locked.txid, locked.vout)
    }

    info = SimpleNamespace(
        base_status="cj-out",
        status="cj-out",
        utxos=[locked, available],
    )

    with patch.object(
        adapter,
        "_address_infos",
        side_effect=[[info], []],
    ):
        result = adapter.get_utxos(mixdepth=0)

    assert [coin.utxo for coin in result] == [available]
    adapter.wallet.get_locked_input_outpoints.assert_called_once_with()


def test_get_utxos_excludes_local_and_cross_process_locks(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()

    local_locked = classified_utxos[0].utxo
    remote_locked = classified_utxos[1].utxo
    available = classified_utxos[2].utxo
    adapter._locked_utxos.add((local_locked.txid, local_locked.vout))
    adapter.wallet.get_locked_input_outpoints.return_value = {
        (remote_locked.txid, remote_locked.vout)
    }

    info = SimpleNamespace(
        base_status="cj-out",
        status="cj-out",
        utxos=[local_locked, remote_locked, available],
    )

    with patch.object(
        adapter,
        "_address_infos",
        side_effect=[[info], []],
    ):
        result = adapter.get_utxos(mixdepth=0)

    assert [coin.utxo for coin in result] == [available]


def test_select_utxos_excludes_cross_process_joinmarket_locks(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.utxo_cache = {
        0: [coin.utxo for coin in classified_utxos[:3]],
    }
    adapter.wallet.get_locked_input_outpoints.return_value = {
        (classified_utxos[0].utxo.txid, classified_utxos[0].utxo.vout),
    }
    adapter.wallet.select_utxos.return_value = [classified_utxos[1].utxo]

    result = adapter.select_utxos(
        mixdepth=0,
        target_amount=50_000,
        allowed_outpoints={
            (coin.utxo.txid, coin.utxo.vout) for coin in classified_utxos[:3]
        },
    )

    assert result == [classified_utxos[1].utxo]
    excluded = adapter.wallet.select_utxos.call_args.kwargs["exclude"]
    assert (classified_utxos[0].utxo.txid, classified_utxos[0].utxo.vout) in excluded


def test_unlock_after_lease_expiry_cannot_release_new_owner(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    class ReservationWallet:
        def __init__(self) -> None:
            self.owners: dict[tuple[str, int], str] = {}

        def reserve_coinjoin_inputs(
            self,
            outpoints: set[tuple[str, int]],
            *,
            ttl: float,
            owner: str,
        ) -> bool:
            del ttl
            if any(outpoint in self.owners for outpoint in outpoints):
                return False
            self.owners.update({outpoint: owner for outpoint in outpoints})
            return True

        def release_coinjoin_inputs(
            self,
            outpoints: set[tuple[str, int]],
            *,
            owner: str,
        ) -> None:
            for outpoint in outpoints:
                if self.owners.get(outpoint) == owner:
                    del self.owners[outpoint]

        def expire(self, outpoint: tuple[str, int]) -> None:
            self.owners.pop(outpoint, None)

    wallet = ReservationWallet()
    adapter_a = JoinMarketAdapter(config=Mock())
    adapter_b = JoinMarketAdapter(config=Mock())
    adapter_a.wallet = cast(WalletService, wallet)
    adapter_b.wallet = cast(WalletService, wallet)

    coin = classified_utxos[0]
    outpoint = (coin.utxo.txid, coin.utxo.vout)

    adapter_a.lock(coin)
    owner_a = adapter_a._lock_owners[outpoint]
    wallet.expire(outpoint)

    adapter_b.lock(coin)
    owner_b = adapter_b._lock_owners[outpoint]
    assert owner_a != owner_b
    assert wallet.owners[outpoint] == owner_b

    # A stale cleanup must compare its owner token. It must not release B's
    # newly acquired reservation after A's lease expired.
    adapter_a.unlock(coin)

    assert wallet.owners[outpoint] == owner_b
    assert outpoint not in adapter_a._lock_owners
    assert outpoint not in adapter_a._locked_utxos
    assert adapter_b._lock_owners[outpoint] == owner_b


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


@pytest.mark.anyio
async def test_get_raw_transaction_returns_backend_transaction() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_transaction = AsyncMock(
        return_value=SimpleNamespace(raw="02000000")
    )

    assert await adapter.get_raw_transaction("11" * 32) == bytes.fromhex("02000000")
    adapter.wallet.backend.get_transaction.assert_awaited_once_with("11" * 32)


@pytest.mark.anyio
async def test_get_raw_transaction_rejects_missing_transaction() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_transaction = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="Unable to retrieve previous transaction"):
        await adapter.get_raw_transaction("11" * 32)


@pytest.mark.anyio
async def test_get_raw_transaction_rejects_invalid_hex() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_transaction = AsyncMock(
        return_value=SimpleNamespace(raw="not-hex")
    )

    with pytest.raises(RuntimeError, match="invalid raw transaction"):
        await adapter.get_raw_transaction("11" * 32)


def test_get_utxos_excludes_unconfirmed_fidelity_and_locked_coins(
    classified_utxos: list[ClassifiedUTXO],
) -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.get_locked_input_outpoints.return_value = set()

    confirmed = classified_utxos[0].utxo
    unconfirmed = SimpleNamespace(
        txid="55" * 32,
        vout=0,
        confirmations=0,
        frozen=False,
        is_fidelity_bond=False,
    )
    fidelity = SimpleNamespace(
        txid="66" * 32,
        vout=0,
        confirmations=6,
        frozen=False,
        is_fidelity_bond=True,
    )
    locked = classified_utxos[1].utxo
    adapter._locked_utxos.add((locked.txid, locked.vout))

    info = SimpleNamespace(
        status="cj-out",
        utxos=[confirmed, unconfirmed, fidelity, locked],
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

    assert [coin.utxo for coin in result] == [confirmed]


@pytest.mark.anyio
async def test_get_mempool_min_fee_returns_none_when_backend_has_no_capability() -> (
    None
):
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend = Mock(spec=[])

    assert await adapter.get_mempool_min_fee() is None


@pytest.mark.anyio
async def test_get_mempool_min_fee_rejects_non_positive_value() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_mempool_min_fee = AsyncMock(return_value=0)

    with pytest.raises(RuntimeError, match="non-positive mempool fee rate"):
        await adapter.get_mempool_min_fee()


@pytest.mark.anyio
async def test_get_mempool_min_fee_rejects_boolean_value() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_mempool_min_fee = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="invalid mempool fee rate"):
        await adapter.get_mempool_min_fee()


@pytest.mark.anyio
async def test_get_mempool_min_fee_rejects_invalid_backend_value() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_mempool_min_fee = AsyncMock(return_value="0.1")

    with pytest.raises(RuntimeError, match="invalid mempool fee rate"):
        await adapter.get_mempool_min_fee()


@pytest.mark.anyio
async def test_get_mempool_min_fee_rejects_non_positive_backend_value() -> None:
    adapter = JoinMarketAdapter(config=Mock())
    adapter.wallet = Mock()
    adapter.wallet.backend.get_mempool_min_fee = AsyncMock(return_value=0)

    with pytest.raises(RuntimeError, match="non-positive mempool fee rate"):
        await adapter.get_mempool_min_fee()
