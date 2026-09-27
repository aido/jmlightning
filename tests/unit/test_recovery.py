from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.recovery import (
    RecoveryJournal,
    RecoveryJournalBusyError,
    RecoveryManager,
)


def test_recovery_journal_is_atomic_and_mode_0600(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("splice", {"channel_id": "02" + "11" * 32})

    assert journal.path.stat().st_mode & 0o777 == 0o600
    data = json.loads(journal.path.read_text())
    assert data[0]["id"] == record_id


def test_recovery_journal_preserves_txid_after_successful_mutation(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("splice", {"channel_id": "channel"})
    txid = "bb" * 32

    journal.call(
        record_id,
        action="splice_signed",
        phase="updated",
        fn=lambda: "ok",
        locked_outpoints=[("aa" * 32, 0)],
        owner_tokens={("aa" * 32, 0): "owner"},
        psbt=b"psbt",
        txid=txid,
    )

    record = journal.records()[0]
    assert record.action is None
    assert record.txid == txid


def test_recovery_journal_keeps_pending_mutation_after_failure(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("splice", {"channel_id": "channel"})

    with pytest.raises(RuntimeError, match="rpc failed"):
        journal.call(
            record_id,
            action="splice_signed",
            phase="updated",
            fn=lambda: (_ for _ in ()).throw(RuntimeError("rpc failed")),
            locked_outpoints=[("aa" * 32, 0)],
            owner_tokens={("aa" * 32, 0): "owner"},
            psbt=b"psbt",
            txid="bb" * 32,
        )

    record = journal.records()[0]
    assert record.action == "splice_signed"
    assert record.owner_tokens[f"{'aa' * 32}:0"] == "owner"
    assert record.psbt is not None
    assert record.txid == "bb" * 32


@pytest.mark.anyio
async def test_recovery_does_not_release_when_bitcoin_backend_has_tx(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path)
    journal.create("splice", {"channel_id": "channel"})
    record = journal.records()[0]
    journal.update(
        record.id,
        locked_outpoints=[("aa" * 32, 0)],
        owner_tokens={f"{'aa' * 32}:0": "owner"},
        txid="bb" * 32,
    )

    config = Mock(data_dir=tmp_path)
    manager = RecoveryManager(config, Path("/run/lightning-rpc"))
    adapter = cast(Any, manager.adapter)
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    setattr(
        adapter,
        "require_wallet",
        Mock(
            return_value=SimpleNamespace(
                backend=SimpleNamespace(
                    get_transaction=AsyncMock(return_value=object())
                )
            )
        ),
    )
    adapter.recover_release = Mock()
    cln = cast(Any, manager.cln)
    cln.get_channel_funding_status = Mock(return_value=ChannelFundingStatus.BROADCAST)

    resolved = await manager.reconcile_all()

    assert resolved == []
    adapter.recover_release.assert_not_called()


@pytest.mark.anyio
async def test_recovery_releases_only_after_cln_and_bitcoin_absent(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("open_channel", {"peer_id": "peer"})
    journal.update(
        record_id,
        locked_outpoints=[("aa" * 32, 0)],
        owner_tokens={f"{'aa' * 32}:0": "owner"},
        txid="bb" * 32,
    )

    config = Mock(data_dir=tmp_path)
    manager = RecoveryManager(config, Path("/run/lightning-rpc"))
    adapter = cast(Any, manager.adapter)
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    setattr(
        adapter,
        "require_wallet",
        Mock(
            return_value=SimpleNamespace(
                backend=SimpleNamespace(get_transaction=AsyncMock(return_value=None))
            )
        ),
    )
    adapter.recover_release = Mock()
    cln = cast(Any, manager.cln)
    cln.get_channel_funding_status = Mock(return_value=ChannelFundingStatus.ABSENT)

    resolved = await manager.reconcile_all()

    assert resolved == [record_id]
    adapter.recover_release.assert_called_once_with(
        ("aa" * 32, 0),
        "owner",
    )
    assert journal.records() == []


@pytest.mark.anyio
async def test_recovery_keeps_all_owner_tokens_across_partial_release(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("open_channel", {"peer_id": "peer"})
    first = "aa" * 32
    second = "bb" * 32
    journal.update(
        record_id,
        locked_outpoints=[(first, 0), (second, 1)],
        owner_tokens={
            f"{first}:0": "owner-a",
            f"{second}:1": "owner-b",
        },
        txid="cc" * 32,
    )

    config = Mock(data_dir=tmp_path)
    manager = RecoveryManager(config, Path("/run/lightning-rpc"))
    adapter = cast(Any, manager.adapter)
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    setattr(
        adapter,
        "require_wallet",
        Mock(
            return_value=SimpleNamespace(
                backend=SimpleNamespace(get_transaction=AsyncMock(return_value=None))
            )
        ),
    )
    adapter.recover_release = Mock(side_effect=[None, RuntimeError("release failed")])
    cln = cast(Any, manager.cln)
    cln.get_channel_funding_status = Mock(return_value=ChannelFundingStatus.ABSENT)

    with pytest.raises(RuntimeError, match="release failed"):
        await manager.reconcile_all()

    record = journal.records()[0]
    assert record.owner_tokens == {
        f"{first}:0": "owner-a",
        f"{second}:1": "owner-b",
    }


@pytest.mark.anyio
async def test_multi_open_recovery_only_cancels_withheld_recorded_funding(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("multi_open_channel", {"peers": ["peer-a", "peer-b"]})
    txid = "bb" * 32
    journal.update(
        record_id,
        locked_outpoints=[("aa" * 32, 0)],
        owner_tokens={f"{'aa' * 32}:0": "owner"},
        txid=txid,
    )

    config = Mock(data_dir=tmp_path)
    manager = RecoveryManager(config, Path("/run/lightning-rpc"))
    adapter = cast(Any, manager.adapter)
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    setattr(
        adapter,
        "require_wallet",
        Mock(
            return_value=SimpleNamespace(
                backend=SimpleNamespace(get_transaction=AsyncMock(return_value=None))
            )
        ),
    )
    adapter.recover_release = Mock()
    cln = cast(Any, manager.cln)
    statuses = {
        "peer-a": ChannelFundingStatus.WITHHELD,
        "peer-b": ChannelFundingStatus.ABSENT,
    }
    cln.get_channel_funding_status = Mock(
        side_effect=lambda peer_id, _txid: statuses[peer_id]
    )

    def cancel(peer_id: str) -> None:
        statuses[peer_id] = ChannelFundingStatus.ABSENT

    cln.cancel_channel_funding = Mock(side_effect=cancel)

    resolved = await manager.reconcile_all()

    assert resolved == [record_id]
    cln.cancel_channel_funding.assert_called_once_with("peer-a")
    adapter.recover_release.assert_called_once_with(("aa" * 32, 0), "owner")


@pytest.mark.anyio
async def test_recovery_does_not_cancel_unidentified_withheld_funding(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("open_channel", {"peer_id": "peer"})
    journal.update(
        record_id,
        locked_outpoints=[("aa" * 32, 0)],
        owner_tokens={f"{'aa' * 32}:0": "owner"},
    )

    config = Mock(data_dir=tmp_path)
    manager = RecoveryManager(config, Path("/run/lightning-rpc"))
    adapter = cast(Any, manager.adapter)
    adapter.connect = AsyncMock()
    adapter.close = AsyncMock()
    adapter.recover_release = Mock()
    cln = cast(Any, manager.cln)
    cln.get_funding_start_status = Mock(return_value=ChannelFundingStatus.WITHHELD)
    cln.cancel_channel_funding = Mock()

    resolved = await manager.reconcile_all()

    assert resolved == []
    cln.cancel_channel_funding.assert_not_called()
    adapter.recover_release.assert_not_called()
    assert journal.records()[0].id == record_id


def test_recovery_journal_lifetime_lock_is_exclusive(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path)
    contender = RecoveryJournal(tmp_path)

    journal.acquire_lifetime()
    try:
        with pytest.raises(RecoveryJournalBusyError, match="owned by a live operation"):
            contender.acquire_lifetime(nonblocking=True)
    finally:
        journal.release_lifetime()

    contender.acquire_lifetime(nonblocking=True)
    contender.release_lifetime()


@pytest.mark.anyio
async def test_recovery_refuses_live_operation(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path)
    journal.acquire_lifetime()
    try:
        config = Mock(data_dir=tmp_path)
        manager = RecoveryManager(config, Path("/run/lightning-rpc"))

        with patch.object(
            manager.adapter, "connect", new_callable=AsyncMock
        ) as mock_connect:
            with pytest.raises(
                RecoveryJournalBusyError, match="owned by a live operation"
            ):
                await manager.reconcile_all()

            mock_connect.assert_not_awaited()
    finally:
        journal.release_lifetime()
