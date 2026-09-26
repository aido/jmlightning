from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.recovery import RecoveryJournal, RecoveryManager


def test_recovery_journal_is_atomic_and_mode_0600(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path)
    record_id = journal.create("splice", {"channel_id": "02" + "11" * 32})

    assert journal.path.stat().st_mode & 0o777 == 0o600
    data = json.loads(journal.path.read_text())
    assert data[0]["id"] == record_id


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
