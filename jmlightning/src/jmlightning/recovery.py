from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import uuid4

from jmlightning.lightning.backend import ChannelFundingStatus

T = TypeVar("T")

if TYPE_CHECKING:
    from jmlightning.config import CLNConfig

JOURNAL_NAME = "recovery.json"


@dataclass
class RecoveryRecord:
    id: str
    operation: str
    identity: dict[str, Any]
    phase: str
    action: str | None = None
    status: str = "pending"
    locked_outpoints: list[tuple[str, int]] = field(default_factory=list)
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
            "locked_outpoints": [list(item) for item in self.locked_outpoints],
            "owner_tokens": self.owner_tokens,
            "psbt": self.psbt,
            "txid": self.txid,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RecoveryRecord:
        raw_outpoints = value.get("locked_outpoints", [])
        outpoints = [
            (str(item[0]), int(item[1]))
            for item in raw_outpoints
            if isinstance(item, list) and len(item) == 2
        ]
        return cls(
            id=str(value["id"]),
            operation=str(value["operation"]),
            identity=dict(value.get("identity", {})),
            phase=str(value.get("phase", "unknown")),
            action=value.get("action"),
            status=str(value.get("status", "pending")),
            locked_outpoints=outpoints,
            owner_tokens={
                str(k): str(v) for k, v in value.get("owner_tokens", {}).items()
            },
            psbt=value.get("psbt"),
            txid=value.get("txid"),
            updated_at=str(value.get("updated_at", "")),
        )


class RecoveryJournal:
    """Small durable journal for operations whose external outcome is ambiguous."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / JOURNAL_NAME
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> list[RecoveryRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read recovery journal {self.path}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Recovery journal {self.path} is invalid")
        return [
            RecoveryRecord.from_dict(item) for item in raw if isinstance(item, dict)
        ]

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
                            value = [tuple(item) for item in value]
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
        locked_outpoints: list[tuple[str, int]] | None = None,
        owner_tokens: dict[tuple[str, int], str] | None = None,
        psbt: bytes | None = None,
        txid: str | None = None,
    ) -> None:
        owners = owner_tokens if isinstance(owner_tokens, dict) else {}
        encoded_owners = {
            f"{txid}:{vout}": owner for (txid, vout), owner in owners.items()
        }
        self.update(
            record_id,
            phase=phase,
            action=action,
            status="pending",
            locked_outpoints=locked_outpoints,
            owner_tokens=encoded_owners,
            psbt=base64.b64encode(psbt).decode("ascii") if psbt is not None else None,
            txid=txid,
        )

    def after_mutation(
        self, record_id: str, *, phase: str, txid: str | None = None
    ) -> None:
        self.update(record_id, phase=phase, action=None, status="pending", txid=txid)

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
        locked_outpoints: list[tuple[str, int]] | None = None,
        owner_tokens: dict[tuple[str, int], str] | None = None,
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

    async def _bitcoin_has_transaction(self, txid: str) -> bool:
        transaction = await self.adapter.require_wallet().backend.get_transaction(txid)
        # Wallet backends return None when the transaction is not known. A
        # transaction object is sufficient evidence that the selected input
        # may already be spent; recovery therefore never releases it.
        return transaction is not None

    async def _reconcile(self, record: RecoveryRecord) -> bool:
        txid = record.txid
        if txid is not None and await self._bitcoin_has_transaction(txid):
            # A Bitcoin backend observation is authoritative enough to keep
            # the reservation. Never release after broadcast.
            return False

        status = await self._cln_status(record)
        if status is ChannelFundingStatus.BROADCAST:
            return False

        if status is ChannelFundingStatus.WITHHELD:
            await self._cancel(record)
            status = await self._cln_status(record)
            if status is not ChannelFundingStatus.ABSENT:
                return False
            if txid is not None and await self._bitcoin_has_transaction(txid):
                return False

        if status is not ChannelFundingStatus.ABSENT:
            return False

        if set(record.owner_tokens) != {
            f"{txid}:{vout}" for txid, vout in record.locked_outpoints
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
                    (txid_text, int(vout_text)): owner,
                },
                psbt=(
                    base64.b64decode(record.psbt) if record.psbt is not None else None
                ),
                txid=record.txid,
            )
            self.adapter.recover_release((txid_text, int(vout_text)), owner)
        return True

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

        peers = identity.get("peers")
        if peers is None:
            peer_id = identity.get("peer_id")
            peers = [peer_id] if isinstance(peer_id, str) else []
        if not isinstance(peers, list) or not all(isinstance(p, str) for p in peers):
            raise RuntimeError("Recovery record has invalid peer ids")
        statuses = (
            [
                self.cln.get_channel_funding_status(peer_id, record.txid)
                for peer_id in peers
            ]
            if record.txid is not None
            else [self.cln.get_funding_start_status(peer_id) for peer_id in peers]
        )
        if any(status is ChannelFundingStatus.BROADCAST for status in statuses):
            return ChannelFundingStatus.BROADCAST
        if any(status is ChannelFundingStatus.WITHHELD for status in statuses):
            return ChannelFundingStatus.WITHHELD
        return ChannelFundingStatus.ABSENT

    async def _cancel(self, record: RecoveryRecord) -> None:
        identity = record.identity
        peers = identity.get("peers")
        if peers is None:
            peer_id = identity.get("peer_id")
            peers = [peer_id] if isinstance(peer_id, str) else []
        for peer_id in peers:
            if not isinstance(peer_id, str):
                raise RuntimeError("Recovery record has invalid peer ids")
            self.journal.before_mutation(
                record.id,
                action="recovery_cancel",
                phase=record.phase,
                locked_outpoints=record.locked_outpoints,
                owner_tokens={
                    (txid, vout): owner
                    for key, owner in record.owner_tokens.items()
                    for txid, vout_text in [key.rsplit(":", 1)]
                    for vout in [int(vout_text)]
                },
                txid=record.txid,
            )
            self.cln.cancel_channel_funding(peer_id)
