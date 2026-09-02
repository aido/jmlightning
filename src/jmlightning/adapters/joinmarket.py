from __future__ import annotations

import logging
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

        # Outpoints locked by jm-lightning during an operation.
        self._locked_utxos: set[tuple[str, int]] = set()

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
        """Close the JoinMarket wallet."""

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

        locked = self._locked_utxos
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

        if outpoint in self._locked_utxos:
            raise ValueError(
                f"UTXO {coin.utxo.txid}:{coin.utxo.vout} is already locked"
            )

        outpoint_ref = f"{coin.utxo.txid}:{coin.utxo.vout}"
        metadata_store = wallet.metadata_store
        owner = uuid4().hex

        if metadata_store is None:
            raise RuntimeError("Cannot lock UTXOs without a data directory")

        if not metadata_store.try_lock_outpoints([outpoint_ref], owner=owner):
            raise ValueError(
                f"UTXO {coin.utxo.txid}:{coin.utxo.vout} is already locked"
            )

        try:
            wallet.freeze_utxo(outpoint_ref)
        except Exception:
            metadata_store.release_outpoints([outpoint_ref], owner=owner)
            raise

        self._locked_utxos.add(outpoint)
        try:
            metadata_store.release_outpoints([outpoint_ref], owner=owner)
        except Exception:
            logger.warning(
                "Failed to release temporary UTXO reservation for %s",
                outpoint_ref,
                exc_info=True,
            )

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

        wallet.unfreeze_utxo(f"{coin.utxo.txid}:{coin.utxo.vout}")

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
        excluded.update(self._locked_utxos)

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
