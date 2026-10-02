from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from jmlightning.lightning.backend import ChannelFundingStatus
from jmlightning.models import Outpoint
from jmlightning.recovery import RecoveryManager, RecoveryRecord

TXID = "11" * 32
INPUT_TXID = "22" * 32
OUTPOINT = Outpoint(INPUT_TXID, 1)
OWNER = "owner-token"


def manager(*, backend_type: str = "descriptor") -> Any:
    recovery = RecoveryManager.__new__(RecoveryManager)
    recovery.adapter = Mock()
    recovery.adapter.config.backend_type = backend_type
    backend = Mock()
    backend.get_transaction = AsyncMock(return_value=None)
    backend.get_mempool_spender = AsyncMock(return_value=None)
    recovery.adapter.require_wallet.return_value.backend = backend
    recovery.cln = Mock()
    recovery.journal = Mock()
    return recovery


def record(
    *,
    id: str = "record-id",
    operation: str = "open_channel",
    identity: dict[str, Any] | None = None,
    phase: str = "funding",
    action: str | None = None,
    status: str = "pending",
    locked_outpoints: list[Outpoint] | None = None,
    owner_tokens: dict[str, str] | None = None,
    psbt: str | None = None,
    txid: str | None = None,
    updated_at: str = "",
) -> RecoveryRecord:
    return RecoveryRecord(
        id=id,
        operation=operation,
        identity={"peer_id": "peer"} if identity is None else identity,
        phase=phase,
        action=action,
        status=status,
        locked_outpoints=[OUTPOINT] if locked_outpoints is None else locked_outpoints,
        owner_tokens={str(OUTPOINT): OWNER} if owner_tokens is None else owner_tokens,
        psbt=psbt,
        txid=txid,
        updated_at=updated_at,
    )


@pytest.mark.anyio
async def test_bitcoin_has_transaction_neutrino_is_indeterminate() -> None:
    recovery = manager(backend_type="neutrino")
    assert await recovery._bitcoin_has_transaction(record(), TXID) is None
    recovery.adapter.require_wallet.assert_not_called()


@pytest.mark.anyio
async def test_bitcoin_has_transaction_finds_transaction() -> None:
    recovery = manager()
    recovery.adapter.require_wallet.return_value.backend.get_transaction = AsyncMock(
        return_value=object()
    )
    assert await recovery._bitcoin_has_transaction(record(), TXID) is True


@pytest.mark.anyio
async def test_bitcoin_has_transaction_finds_spender() -> None:
    recovery = manager()
    backend = recovery.adapter.require_wallet.return_value.backend
    backend.get_transaction = AsyncMock(return_value=None)
    backend.get_mempool_spender = AsyncMock(
        return_value=SimpleNamespace(spending_txid="33" * 32, blockhash=None)
    )
    assert await recovery._bitcoin_has_transaction(record(), TXID) is True


@pytest.mark.anyio
async def test_bitcoin_has_transaction_finds_confirmed_spender() -> None:
    recovery = manager()
    backend = recovery.adapter.require_wallet.return_value.backend
    backend.get_transaction = AsyncMock(return_value=None)
    backend.get_mempool_spender = AsyncMock(
        return_value=SimpleNamespace(spending_txid=None, blockhash="44" * 32)
    )
    assert await recovery._bitcoin_has_transaction(record(), TXID) is True


@pytest.mark.anyio
async def test_bitcoin_has_transaction_spender_lookup_failure_is_indeterminate() -> (
    None
):
    recovery = manager()
    backend = recovery.adapter.require_wallet.return_value.backend
    backend.get_transaction = AsyncMock(return_value=None)
    backend.get_mempool_spender = AsyncMock(side_effect=RuntimeError("rpc failed"))
    assert await recovery._bitcoin_has_transaction(record(), TXID) is None


@pytest.mark.anyio
async def test_bitcoin_has_transaction_absent_and_inputs_unspent() -> None:
    recovery = manager()
    backend = recovery.adapter.require_wallet.return_value.backend
    backend.get_transaction = AsyncMock(return_value=None)
    backend.get_mempool_spender = AsyncMock(return_value=None)
    assert await recovery._bitcoin_has_transaction(record(), TXID) is False


@pytest.mark.anyio
async def test_bitcoin_transaction_confirmed_neutrino_is_indeterminate() -> None:
    recovery = manager(backend_type="neutrino")
    assert await recovery._bitcoin_transaction_confirmed(TXID) is None


@pytest.mark.anyio
async def test_bitcoin_transaction_confirmed_absent_is_indeterminate() -> None:
    recovery = manager()
    recovery.adapter.require_wallet.return_value.backend.get_transaction = AsyncMock(
        return_value=None
    )
    assert await recovery._bitcoin_transaction_confirmed(TXID) is None


@pytest.mark.anyio
async def test_bitcoin_transaction_confirmed_missing_boolean_is_indeterminate() -> None:
    recovery = manager()
    recovery.adapter.require_wallet.return_value.backend.get_transaction = AsyncMock(
        return_value=SimpleNamespace(status=SimpleNamespace(confirmed="yes"))
    )
    assert await recovery._bitcoin_transaction_confirmed(TXID) is None


@pytest.mark.anyio
async def test_bitcoin_transaction_confirmed_returns_boolean() -> None:
    recovery = manager()
    recovery.adapter.require_wallet.return_value.backend.get_transaction = AsyncMock(
        return_value=SimpleNamespace(status=SimpleNamespace(confirmed=True))
    )
    assert await recovery._bitcoin_transaction_confirmed(TXID) is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "rec",
    [
        record(
            operation="peerswap",
            phase="broadcast",
            txid=TXID,
            action="recovery_release",
        ),
        record(operation="peerswap", phase="funding", txid=TXID),
        record(operation="peerswap", phase="broadcast", txid=None),
    ],
)
async def test_reconcile_peerswap_requires_unambiguous_broadcast_record(
    rec: RecoveryRecord,
) -> None:
    recovery = manager()
    assert await recovery._reconcile(rec) is False
    recovery.adapter.recover_release.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("confirmed", [False, None])
async def test_reconcile_peerswap_requires_confirmed_transaction(
    confirmed: bool | None,
) -> None:
    recovery = manager()
    recovery._bitcoin_transaction_confirmed = AsyncMock(return_value=confirmed)
    assert (
        await recovery._reconcile(
            record(operation="peerswap", phase="broadcast", txid=TXID)
        )
        is False
    )
    recovery.adapter.recover_release.assert_not_called()


@pytest.mark.anyio
async def test_reconcile_peerswap_missing_owner_token_is_error() -> None:
    recovery = manager()
    recovery._bitcoin_transaction_confirmed = AsyncMock(return_value=True)
    broken = record(operation="peerswap", phase="broadcast", txid=TXID, owner_tokens={})
    with pytest.raises(RuntimeError, match="missing an owner token"):
        await recovery._reconcile(broken)


@pytest.mark.anyio
async def test_reconcile_peerswap_confirmed_releases_and_resolves() -> None:
    recovery = manager()
    recovery._bitcoin_transaction_confirmed = AsyncMock(return_value=True)
    recovery.journal.before_mutation = Mock()
    recovery.adapter.recover_release = Mock()
    assert (
        await recovery._reconcile(
            record(operation="peerswap", phase="broadcast", txid=TXID)
        )
        is True
    )
    recovery.journal.before_mutation.assert_called_once()
    recovery.adapter.recover_release.assert_called_once_with(OUTPOINT, OWNER)


@pytest.mark.anyio
@pytest.mark.parametrize("bitcoin_result", [True, None])
async def test_reconcile_broadcast_ambiguity_never_releases(
    bitcoin_result: bool | None,
) -> None:
    recovery = manager()
    recovery._bitcoin_has_transaction = AsyncMock(return_value=bitcoin_result)
    recovery._cln_status = AsyncMock()
    assert await recovery._reconcile(record(txid=TXID)) is False
    recovery._cln_status.assert_not_called()
    recovery.adapter.recover_release.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,txid",
    [
        (ChannelFundingStatus.BROADCAST, TXID),
        (ChannelFundingStatus.WITHHELD, None),
        (ChannelFundingStatus.WITHHELD, TXID),
    ],
)
async def test_reconcile_non_absent_statuses_are_not_released(
    status: ChannelFundingStatus,
    txid: str | None,
) -> None:
    recovery = manager()
    recovery._cln_status = AsyncMock(return_value=status)
    assert await recovery._reconcile(record(txid=txid)) is False
    recovery.adapter.recover_release.assert_not_called()


@pytest.mark.anyio
async def test_reconcile_absent_missing_owner_token_is_error() -> None:
    recovery = manager()
    recovery._cln_status = AsyncMock(return_value=ChannelFundingStatus.ABSENT)
    broken = record(owner_tokens={})
    with pytest.raises(RuntimeError, match="missing an owner token"):
        await recovery._reconcile(broken)


@pytest.mark.anyio
async def test_reconcile_rejects_unknown_cln_status() -> None:
    recovery = manager()
    recovery._cln_status = AsyncMock(return_value=object())
    assert await recovery._reconcile(record()) is False
    recovery.adapter.recover_release.assert_not_called()


@pytest.mark.anyio
async def test_reconcile_absent_releases_all_locked_outpoints() -> None:
    second = Outpoint("55" * 32, 2)
    recovery = manager()
    recovery._cln_status = AsyncMock(return_value=ChannelFundingStatus.ABSENT)
    recovery.journal.before_mutation = Mock()
    recovery.adapter.recover_release = Mock()
    rec = record(
        locked_outpoints=[OUTPOINT, second],
        owner_tokens={str(OUTPOINT): "owner-1", str(second): "owner-2"},
    )
    assert await recovery._reconcile(rec) is True
    assert recovery.adapter.recover_release.call_count == 2


@pytest.mark.anyio
async def test_peer_statuses_uses_empty_peers_when_identity_has_no_peer_id() -> None:
    recovery = manager()
    assert await recovery._peer_statuses(record(identity={})) == {}
    recovery.cln.get_funding_start_status.assert_not_called()


@pytest.mark.anyio
async def test_peer_statuses_rejects_non_string_peers_entry() -> None:
    recovery = manager()
    with pytest.raises(RuntimeError, match="invalid peer ids"):
        await recovery._peer_statuses(record(identity={"peers": ["peer", 1]}))


@pytest.mark.anyio
async def test_peer_statuses_uses_peer_id_without_txid() -> None:
    recovery = manager()
    recovery.cln.get_funding_start_status.side_effect = [ChannelFundingStatus.ABSENT]
    result = await recovery._peer_statuses(record(identity={"peer_id": "peer"}))
    assert result == {"peer": ChannelFundingStatus.ABSENT}
    recovery.cln.get_funding_start_status.assert_called_once_with("peer")


@pytest.mark.anyio
async def test_peer_statuses_uses_peers_with_txid() -> None:
    recovery = manager()
    recovery.cln.get_channel_funding_status.side_effect = [
        ChannelFundingStatus.ABSENT,
        ChannelFundingStatus.BROADCAST,
    ]
    result = await recovery._peer_statuses(
        record(identity={"peers": ["peer-1", "peer-2"]}, txid=TXID)
    )
    assert result == {
        "peer-1": ChannelFundingStatus.ABSENT,
        "peer-2": ChannelFundingStatus.BROADCAST,
    }
    recovery.cln.get_channel_funding_status.assert_any_call("peer-1", TXID)
    recovery.cln.get_channel_funding_status.assert_any_call("peer-2", TXID)


@pytest.mark.anyio
async def test_reconcile_all_returns_empty_without_records() -> None:
    recovery = manager()
    recovery.journal.records.return_value = []
    recovery.adapter.connect = AsyncMock()
    recovery.adapter.close = AsyncMock()
    assert await recovery.reconcile_all() == []
    recovery.adapter.connect.assert_not_called()
    recovery.adapter.close.assert_not_called()
    recovery.journal.release_lifetime.assert_called_once()


@pytest.mark.anyio
async def test_reconcile_all_removes_already_resolved_records() -> None:
    recovery = manager()
    resolved = record(status="resolved")
    recovery.journal.records.return_value = [resolved]
    recovery.adapter.connect = AsyncMock()
    recovery.adapter.close = AsyncMock()
    assert await recovery.reconcile_all() == []
    recovery.journal.resolve.assert_called_once_with(resolved.id)
    recovery.adapter.connect.assert_awaited_once()
    recovery.adapter.close.assert_awaited_once()
    recovery.journal.release_lifetime.assert_called_once()


@pytest.mark.anyio
async def test_reconcile_all_resolves_records_reconciled_successfully() -> None:
    recovery = manager()
    pending = record()
    recovery.journal.records.return_value = [pending]
    recovery.adapter.connect = AsyncMock()
    recovery.adapter.close = AsyncMock()
    recovery._reconcile = AsyncMock(return_value=True)
    assert await recovery.reconcile_all() == [pending.id]
    recovery.journal.resolve.assert_called_once_with(pending.id)
    recovery.adapter.close.assert_awaited_once()
    recovery.journal.release_lifetime.assert_called_once()


@pytest.mark.anyio
async def test_reconcile_all_leaves_unresolved_records_pending() -> None:
    recovery = manager()
    pending = record()
    recovery.journal.records.return_value = [pending]
    recovery.adapter.connect = AsyncMock()
    recovery.adapter.close = AsyncMock()
    recovery._reconcile = AsyncMock(return_value=False)
    assert await recovery.reconcile_all() == []
    recovery.journal.resolve.assert_not_called()
    recovery.adapter.close.assert_awaited_once()
    recovery.journal.release_lifetime.assert_called_once()


@pytest.mark.anyio
async def test_cln_status_splice_without_txid_uses_splice_status() -> None:
    recovery = manager()
    recovery.cln.get_splice_funding_status.return_value = ChannelFundingStatus.WITHHELD
    result = await recovery._cln_status(
        record(operation="splice", identity={"channel_id": "channel"})
    )
    assert result is ChannelFundingStatus.WITHHELD
    recovery.cln.get_splice_funding_status.assert_called_once_with("channel")


@pytest.mark.anyio
async def test_cln_status_splice_with_txid_uses_channel_status() -> None:
    recovery = manager()
    recovery.cln.get_channel_funding_status.return_value = ChannelFundingStatus.ABSENT
    result = await recovery._cln_status(
        record(operation="splice", identity={"channel_id": "channel"}, txid=TXID)
    )
    assert result is ChannelFundingStatus.ABSENT
    recovery.cln.get_channel_funding_status.assert_called_once_with("channel", TXID)


@pytest.mark.anyio
async def test_cln_status_splice_requires_channel_id() -> None:
    recovery = manager()
    with pytest.raises(RuntimeError, match="no splice channel id"):
        await recovery._cln_status(record(operation="splice", identity={}))


@pytest.mark.anyio
async def test_cln_status_aggregates_broadcast_before_withheld() -> None:
    recovery = manager()
    recovery._peer_statuses = AsyncMock(
        return_value={
            "peer-1": ChannelFundingStatus.WITHHELD,
            "peer-2": ChannelFundingStatus.BROADCAST,
        }
    )
    assert await recovery._cln_status(record()) is ChannelFundingStatus.BROADCAST


@pytest.mark.anyio
async def test_cln_status_aggregates_withheld_before_absent() -> None:
    recovery = manager()
    recovery._peer_statuses = AsyncMock(
        return_value={
            "peer-1": ChannelFundingStatus.WITHHELD,
            "peer-2": ChannelFundingStatus.ABSENT,
        }
    )
    assert await recovery._cln_status(record()) is ChannelFundingStatus.WITHHELD


@pytest.mark.anyio
async def test_cln_status_empty_or_absent_peers_is_absent() -> None:
    recovery = manager()
    recovery._peer_statuses = AsyncMock(return_value={})
    assert await recovery._cln_status(record()) is ChannelFundingStatus.ABSENT
