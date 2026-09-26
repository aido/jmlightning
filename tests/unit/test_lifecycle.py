from unittest.mock import AsyncMock, Mock

import pytest
from jmwallet.wallet.models import UTXOInfo

from jmlightning.models import ClassifiedUTXO
from jmlightning.operations.lifecycle import LifecyclePhase, OperationLifecycle
from jmlightning.operations.multi_open_channel import MultiOpenChannelPhase
from jmlightning.operations.open_channel import FundingPhase
from jmlightning.operations.peerswap import PeerSwapPhase
from jmlightning.operations.splice import SplicePhase


def _coin() -> ClassifiedUTXO:
    return ClassifiedUTXO(
        utxo=UTXOInfo(
            txid="11" * 32,
            vout=0,
            value=100_000,
            mixdepth=0,
            address="bc1qtest",
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path="m/84'/0'/0'/0/0",
        ),
        status="cj-out",
    )


def test_shared_lifecycle_phase_covers_operation_states() -> None:
    assert LifecyclePhase.PRESTART.value == "prestart"
    assert LifecyclePhase.LOCKED.value == "locked"
    assert LifecyclePhase.STARTED.value == "started"
    assert LifecyclePhase.WITHHELD.value == "withheld"
    assert LifecyclePhase.BROADCAST.value == "broadcast"
    assert LifecyclePhase.PREPARED.value == "prepared"
    assert LifecyclePhase.DISCARDED.value == "discarded"
    assert FundingPhase is LifecyclePhase
    assert MultiOpenChannelPhase is LifecyclePhase
    assert SplicePhase is LifecyclePhase
    assert PeerSwapPhase is LifecyclePhase


@pytest.mark.anyio
async def test_cleanup_collects_unlock_and_close_failures() -> None:
    lifecycle = OperationLifecycle()
    adapter = Mock()
    adapter.unlock.side_effect = RuntimeError("unlock failed")
    adapter.close = AsyncMock(side_effect=RuntimeError("close failed"))

    await lifecycle.cleanup(
        locked=[_coin()],
        adapter=adapter,
        close_message="close failed",
        unlock_message="unlock failed",
    )

    assert len(lifecycle.cleanup_errors) == 2
    adapter.unlock.assert_called_once()
    adapter.close.assert_awaited_once()


def test_lifecycle_rejects_invalid_transition() -> None:
    lifecycle = OperationLifecycle()

    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        lifecycle.transition(LifecyclePhase.BROADCAST)

    lifecycle.transition(LifecyclePhase.LOCKED)
    with pytest.raises(ValueError, match="Invalid lifecycle transition"):
        lifecycle.transition(LifecyclePhase.PREPARED)
