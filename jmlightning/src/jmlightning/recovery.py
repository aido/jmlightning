from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import uuid4

from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.models import Outpoint
from jmlightning.psbt import psbt_from_base64

T = TypeVar("T")

if TYPE_CHECKING:
    from jmlightning.config import CLNConfig

JOURNAL_DIR = "recovery"
JOURNAL_NAME = "recovery.json"


class RecoveryJournalBusyError(RuntimeError):
    """Raised when the recovery journal is owned by a live operation."""


@dataclass
class RecoveryRecord:
    id: str
    operation: str
    identity: dict[str, Any]
    phase: str
    action: str | None = None
    status: str = "pending"
    locked_outpoints: list[Outpoint] = field(default_factory=list)
    owner_tokens: dict[str, str] = field(default_factory=dict)
    psbt: str | None = None
    txid: str | None = None
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "identity": self.identity,
            "phase": self.phase,
            "action": self.action,
            "status": self.status,
            "locked_outpoints": [
                list(item.as_tuple()) for item in self.locked_outpoints
            ],
            "owner_tokens": self.owner_tokens,
            "psbt": self.psbt,
            "txid": self.txid,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RecoveryRecord:
        raw_outpoints = value.get("locked_outpoints", [])
        if not isinstance(raw_outpoints, list):
            raise ValueError("locked_outpoints must be a list")

        outpoints: list[Outpoint] = []
        for item in raw_outpoints:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], int)
                or isinstance(item[1], bool)
            ):
                raise ValueError("invalid locked_outpoints entry")
            outpoints.append(Outpoint(item[0], item[1]))

        identity = value.get("identity", {})
        if not isinstance(identity, dict):
            raise ValueError("identity must be an object")

        owner_tokens = value.get("owner_tokens", {})
        if not isinstance(owner_tokens, dict):
            raise ValueError("owner_tokens must be an object")

        action = value.get("action")
        if action is not None and not isinstance(action, str):
            raise ValueError("action must be a string or null")

        psbt = value.get("psbt")
        if psbt is not None and not isinstance(psbt, str):
            raise ValueError("psbt must be a string or null")

        txid = value.get("txid")
        if txid is not None and not isinstance(txid, str):
            raise ValueError("txid must be a string or null")

        return cls(
            id=str(value["id"]),
            operation=str(value["operation"]),
            identity=identity,
            phase=str(value.get("phase", "unknown")),
            action=action,
            status=str(value.get("status", "pending")),
            locked_outpoints=outpoints,
            owner_tokens={str(k): str(v) for k, v in owner_tokens.items()},
            psbt=psbt,
            txid=txid,
            updated_at=str(value.get("updated_at", "")),
        )


class RecoveryJournal:
    """Small durable journal for operations whose external outcome is ambiguous."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / JOURNAL_DIR / JOURNAL_NAME
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._lock_path = self.path.with_suffix(".lock")
        self._lifetime_fd: int | None = None
        # Serialise journal access between threads in this process. The
        # lifetime file lock only provides inter-process exclusion; without a
        # process-local lock, the renewal worker can race an operation update
        # and overwrite a newer recovery record with an older snapshot.
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if self._lifetime_fd is not None:
                yield
                return

            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def acquire_lifetime(self, *, nonblocking: bool = False) -> None:
        """Own the journal lock for a complete recoverable lifecycle."""
        if self._lifetime_fd is not None:
            raise RuntimeError("Recovery journal lifetime lock is already held")

        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            flags = fcntl.LOCK_EX
            if nonblocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                raise RecoveryJournalBusyError(
                    "Recovery journal is owned by a live operation"
                ) from exc
            self._lifetime_fd = fd
        except Exception:
            os.close(fd)
            raise

    def release_lifetime(self) -> None:
        """Release a lifetime journal lock owned by this journal instance."""
        fd = self._lifetime_fd
        if fd is None:
            return
        self._lifetime_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> list[RecoveryRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read recovery journal {self.path}") from exc
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise RuntimeError(f"Recovery journal {self.path} is invalid")
        try:
            return [RecoveryRecord.from_dict(item) for item in raw]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Recovery journal {self.path} is invalid") from exc

    def _write(self, records: list[RecoveryRecord]) -> None:
        payload = json.dumps(
            [record.to_dict() for record in records],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fd, name = tempfile.mkstemp(prefix=".recovery.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def begin(self, operation: str, identity: dict[str, Any]) -> str:
        """Create a recovery record while taking lifetime ownership."""
        self.acquire_lifetime()
        try:
            return self.create(operation, identity)
        except Exception:
            self.release_lifetime()
            raise

    def create(self, operation: str, identity: dict[str, Any]) -> str:
        record = RecoveryRecord(
            id=uuid4().hex,
            operation=operation,
            identity=identity,
            phase="prestart",
            updated_at=datetime.now(UTC).isoformat(),
        )
        with self._locked():
            records = self._load()
            records.append(record)
            self._write(records)
        return record.id

    def update(self, record_id: str, **changes: Any) -> None:
        with self._locked():
            records = self._load()
            for record in records:
                if record.id == record_id:
                    for key, value in changes.items():
                        if key == "locked_outpoints" and value is not None:
                            value = [
                                item
                                if isinstance(item, Outpoint)
                                else Outpoint(item[0], item[1])
                                for item in value
                            ]
                        setattr(record, key, value)
                    record.updated_at = datetime.now(UTC).isoformat()
                    self._write(records)
                    return
        raise KeyError(f"Unknown recovery record {record_id}")

    def before_mutation(
        self,
        record_id: str,
        *,
        action: str,
        phase: str,
        locked_outpoints: list[Outpoint] | None = None,
        owner_tokens: dict[Outpoint, str] | None = None,
        psbt: bytes | None = None,
        txid: str | None = None,
    ) -> None:
        owners = owner_tokens if isinstance(owner_tokens, dict) else {}
        encoded_owners = {str(outpoint): owner for outpoint, owner in owners.items()}
        changes: dict[str, Any] = {
            "phase": phase,
            "action": action,
            "status": "pending",
            "locked_outpoints": locked_outpoints,
            "owner_tokens": encoded_owners,
        }
        # Reservation renewals do not carry the transaction metadata from the
        # surrounding operation. Preserve existing values instead of clearing
        # the recovery identity for a transaction which may already have been
        # handed to CLN.
        if psbt is not None:
            changes["psbt"] = base64.b64encode(psbt).decode("ascii")
        if txid is not None:
            changes["txid"] = txid
        self.update(record_id, **changes)

    def after_mutation(
        self, record_id: str, *, phase: str, txid: str | None = None
    ) -> None:
        changes: dict[str, Any] = {
            "phase": phase,
            "action": None,
            "status": "pending",
        }
        # A reservation renewal does not know the operation's transaction id.
        # Preserve an existing txid instead of clearing it, so a long-running
        # operation remains recoverable even while its JoinMarket lease is
        # being renewed.
        if txid is not None:
            changes["txid"] = txid
        self.update(record_id, **changes)

    def resolve(self, record_id: str) -> None:
        with self._locked():
            records = [record for record in self._load() if record.id != record_id]
            self._write(records)

    def records(self) -> list[RecoveryRecord]:
        return self._load()

    def call(
        self,
        record_id: str,
        *,
        action: str,
        phase: str,
        fn: Callable[[], T],
        locked_outpoints: list[Outpoint] | None = None,
        owner_tokens: dict[Outpoint, str] | None = None,
        psbt: bytes | None = None,
        txid: str | None = None,
    ) -> T:
        self.before_mutation(
            record_id,
            action=action,
            phase=phase,
            locked_outpoints=locked_outpoints,
            owner_tokens=owner_tokens,
            psbt=psbt,
            txid=txid,
        )
        result = fn()
        self.after_mutation(record_id, phase=phase, txid=txid)
        return result


class RecoveryManager:
    """Reconcile durable recovery records using CLN and Bitcoin state."""

    def __init__(self, config: CLNConfig, cln_socket: Path) -> None:
        # Keep these imports local. RecoveryJournal is imported by the JoinMarket
        # adapter, so importing the adapter at module scope here would create a
        # recovery -> adapter -> recovery cycle.
        from jmlightning.adapters.joinmarket import JoinMarketAdapter
        from jmlightning.lightning.cln import CLNBackend

        self.journal = RecoveryJournal(cast(Path, config.data_dir))
        self.adapter = JoinMarketAdapter(config=config)
        self.cln = CLNBackend(str(cln_socket))

    async def reconcile_all(self) -> list[str]:
        self.journal.acquire_lifetime(nonblocking=True)
        try:
            records = self.journal.records()
            if not records:
                return []
            await self.adapter.connect()
            resolved: list[str] = []
            try:
                for record in records:
                    if record.status == "resolved":
                        self.journal.resolve(record.id)
                        continue
                    if await self._reconcile(record):
                        self.journal.resolve(record.id)
                        resolved.append(record.id)
            finally:
                await self.adapter.close()
            return resolved
        finally:
            self.journal.release_lifetime()

    async def _bitcoin_has_transaction(
        self, record: RecoveryRecord, txid: str
    ) -> bool | None:
        # Neutrino cannot establish transaction absence: its get_transaction()
        # lookup is limited to transactions observed in the watched mempool and
        # returns None for confirmed transactions as well as unknown ones.
        # Treat an unknown result as indeterminate rather than releasing a
        # reservation after a transaction was actually broadcast.
        if self.adapter.config.backend_type == "neutrino":
            return None

        backend = self.adapter.require_wallet().backend
        transaction = await backend.get_transaction(txid)
        if transaction is not None:
            # A transaction object is sufficient evidence that the selected input
            # may already be spent; recovery therefore never releases it.
            return True

        # JoinMarket-NG's descriptor backend deliberately returns None for both
        # an absent transaction and backend/RPC failures. Do not interpret that
        # ambiguous result as proof that the transaction was never broadcast.
        # Its authoritative mempool-spender lookup lets us instead inspect the
        # recorded inputs directly. A current spender, including a confirmed
        # spender reported by Bitcoin Core, proves that the reservation must be
        # retained. A clean result for every recorded input proves that the
        # recorded transaction cannot currently be spending any of them.
        for outpoint in record.locked_outpoints:
            try:
                spender = await backend.get_mempool_spender(
                    outpoint.txid, outpoint.vout
                )
            except Exception:
                return None
            if (
                getattr(spender, "spending_txid", None) is not None
                or getattr(spender, "blockhash", None) is not None
            ):
                return True

        return False

    async def _bitcoin_transaction_confirmed(self, txid: str) -> bool | None:
        if self.adapter.config.backend_type == "neutrino":
            return None
        transaction = await self.adapter.require_wallet().backend.get_transaction(txid)
        if transaction is None:
            return None
        status = getattr(transaction, "status", None)
        confirmed = getattr(status, "confirmed", None)
        return confirmed if isinstance(confirmed, bool) else None

    async def _reconcile(self, record: RecoveryRecord) -> bool:
        txid = record.txid
        if record.operation == "peerswap":
            # A txsend call which has not reached after_mutation is inherently
            # ambiguous: a broadcast RPC failure or process crash cannot prove
            # that no transaction reached the network. Never release that
            # reservation automatically.
            if record.action is not None or record.phase != "broadcast" or txid is None:
                return False
            confirmed = await self._bitcoin_transaction_confirmed(txid)
            if confirmed is not True:
                return False

            if set(record.owner_tokens) != {
                str(outpoint) for outpoint in record.locked_outpoints
            }:
                raise RuntimeError(
                    "Recovery record is missing an owner token for a locked outpoint"
                )
            for key, owner in record.owner_tokens.items():
                out_txid, vout_text = key.rsplit(":", 1)
                self.journal.before_mutation(
                    record.id,
                    action="recovery_release",
                    phase=record.phase,
                    locked_outpoints=record.locked_outpoints,
                    owner_tokens={
                        Outpoint(owner_txid, int(owner_vout)): owner_token
                        for owner_key, owner_token in record.owner_tokens.items()
                        for owner_txid, owner_vout in [owner_key.rsplit(":", 1)]
                    },
                    psbt=(
                        psbt_from_base64(record.psbt)
                        if record.psbt is not None
                        else None
                    ),
                    txid=record.txid,
                )
                self.adapter.recover_release(Outpoint(out_txid, int(vout_text)), owner)
            return True

        if txid is not None and record.operation != "peerswap":
            bitcoin_has_transaction = await self._bitcoin_has_transaction(record, txid)
            if bitcoin_has_transaction is not False:
                # A Bitcoin backend observation is either evidence that the
                # transaction exists or that its absence cannot be established.
                # In both cases, never release after an ambiguous broadcast.
                return False

        status = await self._cln_status(record)
        if status is ChannelFundingStatus.BROADCAST:
            return False

        if status is ChannelFundingStatus.WITHHELD:
            # Without a transaction id, a currently withheld funding candidate
            # cannot be tied to this recovery record. It may be a later
            # operation for the same peer/channel after the original operation
            # was cancelled externally. Cancelling it here could therefore
            # interfere with an unrelated operation. Leave the record pending
            # for explicit operator reconciliation instead.
            if txid is None:
                return False

            # fundchannel_cancel is scoped only by peer, not by transaction id.
            # A recovery record therefore cannot safely cancel the exact funding
            # operation it describes: a newer funding attempt for the same peer
            # could have replaced it between the status check and cancellation.
            # Leave withheld funding pending for explicit operator reconciliation.
            return False

        if status is not ChannelFundingStatus.ABSENT:
            return False

        if set(record.owner_tokens) != {
            str(outpoint) for outpoint in record.locked_outpoints
        }:
            raise RuntimeError(
                "Recovery record is missing an owner token for a locked outpoint"
            )

        for key, owner in record.owner_tokens.items():
            txid_text, vout_text = key.rsplit(":", 1)
            self.journal.before_mutation(
                record.id,
                action="recovery_release",
                phase=record.phase,
                locked_outpoints=record.locked_outpoints,
                owner_tokens={
                    Outpoint(owner_txid, int(owner_vout)): owner_token
                    for owner_key, owner_token in record.owner_tokens.items()
                    for owner_txid, owner_vout in [owner_key.rsplit(":", 1)]
                },
                psbt=(
                    psbt_from_base64(record.psbt) if record.psbt is not None else None
                ),
                txid=record.txid,
            )
            self.adapter.recover_release(Outpoint(txid_text, int(vout_text)), owner)
        return True

    async def _peer_statuses(
        self, record: RecoveryRecord
    ) -> dict[str, ChannelFundingStatus]:
        identity = record.identity
        peers = identity.get("peers")
        if peers is None:
            peer_id = identity.get("peer_id")
            peers = [peer_id] if isinstance(peer_id, str) else []
        if not isinstance(peers, list) or not all(isinstance(p, str) for p in peers):
            raise RuntimeError("Recovery record has invalid peer ids")
        if record.txid is not None:
            return {
                peer_id: self.cln.get_channel_funding_status(peer_id, record.txid)
                for peer_id in peers
            }
        return {
            peer_id: self.cln.get_funding_start_status(peer_id) for peer_id in peers
        }

    async def _cln_status(self, record: RecoveryRecord) -> ChannelFundingStatus:
        from jmlightning.lightning.backend import ChannelFundingStatus

        identity = record.identity
        if record.operation == "splice":
            channel_id = identity.get("channel_id")
            if not isinstance(channel_id, str):
                raise RuntimeError("Recovery record has no splice channel id")
            if record.txid is None:
                return self.cln.get_splice_funding_status(channel_id)
            return self.cln.get_channel_funding_status(channel_id, record.txid)

        statuses = (await self._peer_statuses(record)).values()
        if any(status is ChannelFundingStatus.BROADCAST for status in statuses):
            return ChannelFundingStatus.BROADCAST
        if any(status is ChannelFundingStatus.WITHHELD for status in statuses):
            return ChannelFundingStatus.WITHHELD
        return ChannelFundingStatus.ABSENT
