from __future__ import annotations

import asyncio
import logging
import threading
from typing import Protocol
from uuid import uuid4

import jmwallet.backends.descriptor_wallet as descriptor_wallet
import jmwallet.backends.neutrino as neutrino_backend
from jmcore.bitcoin import ParsedTransaction, serialize_transaction
from jmwallet.wallet.display import WalletDisplayMixin
from jmwallet.wallet.models import AddressInfo, UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signer import SignedInput

from jmlightning.config import CLNConfig
from jmlightning.models import ClassifiedUTXO

logger = logging.getLogger(__name__)

# Temporary jm-lightning reservations are JoinMarket CoinJoin-style leases.
# They must not use the persistent user-facing UTXO freeze mechanism: a lease
# may expire and be acquired by another process, while a stale process must
# still be unable to release the newer owner's reservation.
#
# Keep the lease longer than the normal interactive funding flow. Operations
# which can legitimately outlive this window should renew their reservations
# before continuing.
LOCK_TTL_SECONDS = 30 * 60
# Renew frequently enough that a transient scheduler/backend delay does not
# allow a long-lived operation to lose its reservation between checkpoints.
LOCK_RENEWAL_INTERVAL_SECONDS = 5 * 60


class NetworkLike(Protocol):
    @property
    def value(self) -> str: ...


Backend = descriptor_wallet.DescriptorWalletBackend | neutrino_backend.NeutrinoBackend


class JoinMarketAdapter:
    """
    JoinMarket integration boundary.

    Owns the JoinMarket WalletService and exposes only the wallet
    operations required by jm-lightning.

    Policy decisions are deliberately kept outside this adapter.
    """

    def __init__(self, config: CLNConfig):
        self.config = config
        self.wallet: WalletService | None = None

        # Outpoints reserved by this adapter during an operation. The
        # persisted JoinMarket reservation is the authoritative cross-process
        # lock. This local set only prevents duplicate acquisition within this
        # adapter instance.
        self._locked_utxos: set[tuple[str, int]] = set()
        # Owner generations are retained until the corresponding reservation
        # is successfully released. JoinMarket compares this owner token when
        # releasing a reservation, so an expired stale owner cannot release a
        # newer owner's reservation.
        self._lock_owners: dict[tuple[str, int], str] = {}
        # Reservation renewal must not depend on the asyncio event loop.
        # jm-lightning is a oneshot command and some of its synchronous CLN
        # operations can legitimately occupy the loop for longer than the
        # renewal interval. A dedicated daemon thread keeps the lease alive
        # even while the event loop is not scheduling Python tasks.
        self._renewal_thread: threading.Thread | None = None
        self._renewal_stop = threading.Event()
        self._renewal_error: RuntimeError | None = None
        self._lock_state = threading.Lock()
        # WalletService's metadata store is process-safe but its in-memory
        # record cache is not a thread-safe interface. Serialise reservation
        # mutations made by the main operation and the renewal worker.
        self._reservation_io = threading.Lock()

    async def get_raw_transaction(self, txid: str) -> bytes:
        """Return the complete raw transaction for a JoinMarket UTXO."""
        wallet = self._require_wallet()
        transaction = await wallet.backend.get_transaction(txid)

        if transaction is None or not transaction.raw:
            raise RuntimeError(
                f"Unable to retrieve previous transaction for UTXO {txid}"
            )

        try:
            return bytes.fromhex(transaction.raw)
        except ValueError as exc:
            raise RuntimeError(
                f"JoinMarket backend returned invalid raw transaction for {txid}"
            ) from exc

    def sign_input(
        self,
        tx: ParsedTransaction,
        input_index: int,
        utxo: UTXOInfo,
    ) -> SignedInput:
        wallet = self._require_wallet()

        return wallet.sign_input(
            tx=tx,
            input_index=input_index,
            utxo=utxo,
        )

    async def connect(self) -> None:
        """Create and synchronise the JoinMarket wallet."""

        bitcoin_network = self.config.bitcoin_network

        if bitcoin_network is None:
            raise RuntimeError(
                "bitcoin_network must be configured for JoinMarket wallet"
            )

        backend = self._create_backend(bitcoin_network)

        if self.config.backend_type == "neutrino":
            if not isinstance(
                backend,
                neutrino_backend.NeutrinoBackend,
            ):
                raise RuntimeError("Expected Neutrino backend")

            synced = await backend.wait_for_sync(timeout=30.0)

            if not synced:
                raise RuntimeError("Neutrino backend did not sync")
        else:
            await backend.get_block_height()

        self.wallet = WalletService(
            mnemonic=self.config.mnemonic.get_secret_value(),
            passphrase=self.config.passphrase.get_secret_value(),
            backend=backend,
            network=bitcoin_network.value,
            mixdepth_count=self.config.mixdepth_count,
            gap_limit=self.config.gap_limit,
            scan_range=self.config.scan_range,
            data_dir=self.config.data_dir,
            max_sats_freeze_reuse=self.config.max_sats_freeze_reuse,
            reconstruct_history=self.config.reconstruct_history,
        )

        await self.wallet.sync_with_registered_bonds()

    async def close(self) -> None:
        """Close the JoinMarket wallet and stop reservation renewal."""

        await self.stop_lock_renewal()

        if self.wallet is not None:
            await self.wallet.close()
            self.wallet = None

    def _require_wallet(self) -> WalletService:
        if self.wallet is None:
            raise RuntimeError("JoinMarketAdapter is not connected")

        return self.wallet

    def _create_backend(self, bitcoin_network: NetworkLike) -> Backend:
        backend_config = self.config.backend_config

        backend: Backend

        if self.config.backend_type == "neutrino":
            backend = neutrino_backend.NeutrinoBackend(
                neutrino_url=backend_config["neutrino_url"],
                network=bitcoin_network.value,
                scan_start_height=backend_config.get("scan_start_height"),
                add_peers=backend_config.get("add_peers"),
                tls_cert_path=backend_config.get("tls_cert_path"),
                auth_token=backend_config.get("auth_token"),
                include_mempool=backend_config.get("include_mempool", True),
                fee_estimate_url=backend_config.get("fee_estimate_url"),
                fee_estimate_proxy=backend_config.get("fee_estimate_proxy"),
            )

        elif self.config.backend_type == "descriptor_wallet":
            mnemonic = self.config.mnemonic.get_secret_value()
            passphrase = self.config.passphrase.get_secret_value()

            fingerprint = descriptor_wallet.get_mnemonic_fingerprint(
                mnemonic,
                passphrase,
            )

            wallet_name = descriptor_wallet.generate_wallet_name(
                fingerprint,
                bitcoin_network.value,
            )

            backend = descriptor_wallet.DescriptorWalletBackend(
                rpc_url=backend_config["rpc_url"],
                rpc_user=backend_config["rpc_user"],
                rpc_password=backend_config["rpc_password"],
                wallet_name=wallet_name,
            )

        else:
            raise ValueError(f"Unknown backend type: {self.config.backend_type}")

        creation_height = self.config.creation_height

        if creation_height is not None:
            backend.set_wallet_creation_height(creation_height)

        return backend

    def _address_infos(
        self,
        mixdepth: int,
        change: int,
    ) -> list[AddressInfo]:
        wallet = self._require_wallet()

        return WalletDisplayMixin.get_address_info_for_mixdepth(
            wallet,
            mixdepth,
            change,
        )

    def require_wallet(self) -> WalletService:
        """Return the connected wallet service."""
        return self._require_wallet()

    async def get_mempool_min_fee(self) -> float | None:
        """Return the Bitcoin node mempool minimum fee in sat/vB, if available."""
        wallet = self._require_wallet()
        getter = getattr(wallet.backend, "get_mempool_min_fee", None)
        if not callable(getter):
            return None

        fee_rate = await getter()
        if fee_rate is None:
            return None
        if not isinstance(fee_rate, (int, float)) or isinstance(fee_rate, bool):
            raise RuntimeError(
                "JoinMarket backend returned an invalid mempool fee rate"
            )
        if fee_rate <= 0:
            raise RuntimeError(
                "JoinMarket backend returned a non-positive mempool fee rate"
            )
        return float(fee_rate)

    def get_utxos(
        self,
        mixdepth: int,
    ) -> list[ClassifiedUTXO]:
        """
        Return UTXOs that are technically available for selection.

        The adapter deliberately does NOT apply jm-lightning policy here.

        It only removes coins that should never be offered to the
        transaction-selection layer, such as:
        - fidelity bonds
        - already locked UTXOs
        - unconfirmed UTXOs
        """

        # JoinMarket-NG persists temporary input reservations in the wallet
        # metadata store and exposes them through get_locked_input_outpoints().
        # Re-read them here so reservations made by another process (or by
        # JoinMarket's own maker/taker code) are excluded before selection.
        wallet = self._require_wallet()
        with self._lock_state:
            locked = set(self._locked_utxos)
        locked.update(wallet.get_locked_input_outpoints())
        classified_utxos: list[ClassifiedUTXO] = []

        for change in (0, 1):
            for info in self._address_infos(
                mixdepth,
                change,
            ):
                status = info.status

                for utxo in info.utxos:
                    # Never expose user-frozen UTXOs to operations.
                    if utxo.frozen:
                        continue

                    outpoint = (
                        utxo.txid,
                        utxo.vout,
                    )

                    # Never expose a UTXO that is already locked
                    # by another jm-lightning operation.
                    if outpoint in locked:
                        continue

                    # Only confirmed UTXOs are candidates for funding.
                    #
                    # This is an operational wallet constraint rather
                    # than a jm-lightning privacy-policy decision.
                    if getattr(utxo, "confirmations", 0) < 1:
                        continue

                    # Fidelity bonds are not funding inputs.
                    #
                    # Depending on the JM version, this may already be
                    # excluded by the address/UTXO enumeration. The
                    # explicit check keeps the adapter boundary clear.
                    if getattr(utxo, "is_fidelity_bond", False):
                        continue

                    classified_utxos.append(
                        ClassifiedUTXO(
                            utxo=utxo,
                            status=status,
                        )
                    )

        return classified_utxos

    def lock(
        self,
        coin: ClassifiedUTXO,
    ) -> None:
        """
        Lock a selected UTXO in JoinMarket.

        The CLI decides which coins are acceptable. Once it has made
        that decision, it hands the selected coin back to the adapter
        for locking.
        """

        wallet = self._require_wallet()

        outpoint = (
            coin.utxo.txid,
            coin.utxo.vout,
        )

        with self._lock_state:
            already_locked = outpoint in self._locked_utxos
        if already_locked:
            raise ValueError(
                f"UTXO {coin.utxo.txid}:{coin.utxo.vout} is already locked"
            )

        owner = uuid4().hex

        # Use JoinMarket-NG's owned CoinJoin-style reservation API. Do not
        # call freeze_utxo(): freezes are persistent user state, whereas this
        # reservation is a temporary lease. Mixing the two lifetimes means an
        # expired lease can be reacquired by another process while a stale
        # cleanup path can still unfreeze the new owner's UTXO.
        with self._reservation_io:
            reserved = wallet.reserve_coinjoin_inputs(
                {outpoint},
                ttl=LOCK_TTL_SECONDS,
                owner=owner,
            )
        if not reserved:
            raise ValueError(
                f"UTXO {coin.utxo.txid}:{coin.utxo.vout} is already locked"
            )

        with self._lock_state:
            self._locked_utxos.add(outpoint)
            self._lock_owners[outpoint] = owner

    def start_lock_renewal(self) -> None:
        """Start renewal independently of the asyncio event loop.

        The renewal worker is a daemon thread because this is a oneshot
        command rather than a long-running service. It is intentionally not
        implemented as an asyncio task: a synchronous operation can starve
        the event loop and otherwise prevent the renewal task from running
        before the lease expires.
        """
        self._require_wallet()

        with self._lock_state:
            if not self._lock_owners:
                return

            if self._renewal_error is not None:
                raise RuntimeError(
                    "JoinMarket reservation renewal previously failed"
                ) from self._renewal_error

            if self._renewal_thread is not None and self._renewal_thread.is_alive():
                return

        # Establish a fresh full lease before the worker starts waiting.
        self._renew_owned_outpoints()

        self._renewal_stop.clear()
        thread = threading.Thread(
            target=self._renew_locks_periodically,
            name="jmlightning-joinmarket-lock-renewal",
            daemon=True,
        )
        self._renewal_thread = thread
        thread.start()

    def _renew_locks_periodically(self) -> None:
        """Keep owned reservations alive without relying on asyncio."""
        while not self._renewal_stop.wait(LOCK_RENEWAL_INTERVAL_SECONDS):
            with self._lock_state:
                if not self._lock_owners:
                    return
            try:
                self._renew_owned_outpoints()
            except Exception as exc:
                self._renewal_error = RuntimeError(
                    "JoinMarket reservation renewal failed; "
                    "the operation must not continue with these inputs"
                )
                self._renewal_error.__cause__ = exc
                logger.error(
                    "JoinMarket reservation renewal failed; "
                    "automatic renewal has stopped: %s",
                    exc,
                )
                return

    def _renew_owned_outpoints(self) -> None:
        """Renew every currently owned reservation."""
        wallet = self._require_wallet()
        with self._lock_state:
            owners = list(self._lock_owners.items())
        for outpoint, owner in owners:
            with self._reservation_io:
                renewed = wallet.renew_coinjoin_inputs(
                    {outpoint},
                    owner=owner,
                    ttl=LOCK_TTL_SECONDS,
                )
            if not renewed:
                raise RuntimeError(
                    f"JoinMarket reservation for {outpoint[0]}:{outpoint[1]} "
                    "expired or is no longer owned by this operation"
                )

    async def stop_lock_renewal(self) -> None:
        """Stop background renewal without releasing reservations."""
        thread = self._renewal_thread
        self._renewal_thread = None
        if thread is None:
            return

        self._renewal_stop.set()
        # The worker only performs synchronous metadata I/O. Joining it off
        # the event loop avoids turning shutdown into another event-loop
        # dependency while still guaranteeing the worker cannot race wallet
        # shutdown.
        await asyncio.to_thread(thread.join)

    def renew(
        self,
        coin: ClassifiedUTXO,
    ) -> None:
        """Renew a JoinMarket reservation owned by this adapter.

        JoinMarket renews the lease atomically only when the persisted
        reservation still belongs to our owner generation. A ``False``
        result therefore means that the lease can no longer be proven to
        be ours, either because it expired or because another generation
        acquired the outpoint. Never continue an operation after that.
        """
        wallet = self._require_wallet()

        outpoint = (
            coin.utxo.txid,
            coin.utxo.vout,
        )
        with self._lock_state:
            owner = self._lock_owners.get(outpoint)
        if owner is None:
            raise RuntimeError(
                f"UTXO {coin.utxo.txid}:{coin.utxo.vout} has no local "
                "JoinMarket reservation owner"
            )

        with self._reservation_io:
            renewed = wallet.renew_coinjoin_inputs(
                {outpoint},
                owner=owner,
                ttl=LOCK_TTL_SECONDS,
            )
        if not renewed:
            raise RuntimeError(
                f"JoinMarket reservation for {coin.utxo.txid}:{coin.utxo.vout} "
                "expired or is no longer owned by this operation"
            )

    def renew_locks(
        self,
        coins: list[ClassifiedUTXO],
    ) -> None:
        """Renew all JoinMarket reservations owned by this adapter.

        Each UTXO has its own owner generation, so JoinMarket's per-owner
        renewal API must be called separately for each outpoint.
        """
        if self._renewal_error is not None:
            raise RuntimeError(
                "JoinMarket reservation renewal previously failed"
            ) from self._renewal_error

        for coin in coins:
            self.renew(coin)

    def unlock(
        self,
        coin: ClassifiedUTXO,
    ) -> None:
        """Unlock a UTXO previously locked by jm-lightning."""

        wallet = self._require_wallet()

        outpoint = (
            coin.utxo.txid,
            coin.utxo.vout,
        )

        with self._lock_state:
            owner = self._lock_owners.get(outpoint)
        if owner is None:
            # Never unfreeze or otherwise mutate an untracked UTXO. A UTXO
            # without a local owner may be user-frozen or may belong to a
            # different process. Ownership-safe release is the only cleanup
            # operation this adapter performs.
            return

        try:
            with self._reservation_io:
                wallet.release_coinjoin_inputs({outpoint}, owner=owner)
        except Exception:
            # Retain both the owner and local reservation until cleanup can be
            # retried. In particular, do not drop the owner after a failed
            # release or a later cleanup could lose the compare-and-release
            # token.
            raise

        with self._lock_state:
            self._lock_owners.pop(outpoint, None)
            self._locked_utxos.discard(outpoint)

    def get_change_address(
        self,
        mixdepth: int,
    ) -> str:
        wallet = self._require_wallet()

        return wallet.get_new_internal_address(mixdepth)

    def select_utxos(
        self,
        mixdepth: int,
        target_amount: int,
        allowed_outpoints: set[tuple[str, int]],
    ) -> list[UTXOInfo]:
        """Delegate coin-selection mathematics to JoinMarket.

        Every UTXO outside ``allowed_outpoints`` is explicitly excluded.
        """
        wallet = self._require_wallet()

        all_utxos = wallet.utxo_cache.get(mixdepth, [])

        excluded = {
            (utxo.txid, utxo.vout)
            for utxo in all_utxos
            if (utxo.txid, utxo.vout) not in allowed_outpoints
        }
        # Apply the authoritative cross-process reservation set at the
        # selection boundary too. get_utxos() filters these coins for normal
        # callers, but explicit allowed_outpoints can otherwise bypass that
        # filtering.
        with self._lock_state:
            excluded.update(self._locked_utxos)
        excluded.update(wallet.get_locked_input_outpoints())

        return wallet.select_utxos(
            mixdepth=mixdepth,
            target_amount=target_amount,
            exclude=excluded,
            include_fidelity_bonds=False,
        )

    async def broadcast(
        self,
        tx: ParsedTransaction,
    ) -> str:
        wallet = self._require_wallet()

        tx_hex = serialize_transaction(
            tx.version,
            tx.inputs,
            tx.outputs,
            tx.locktime,
            tx.witnesses,
        ).hex()

        return await wallet.backend.broadcast_transaction(tx_hex)
