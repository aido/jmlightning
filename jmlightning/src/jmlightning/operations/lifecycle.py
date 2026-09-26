from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import TypeVar

from loguru import logger

from jmlightning.adapters.joinmarket import JoinMarketAdapter
from jmlightning.models import ClassifiedUTXO
from jmlightning.recovery import RecoveryJournal


class LifecyclePhase(StrEnum):
    """Durable lifecycle phases shared by all external-funding operations."""

    PRESTART = auto()
    LOCKED = auto()
    STARTED = auto()
    WITHHELD = auto()
    UPDATED = auto()
    SIGNED = auto()
    PREPARED = auto()
    BROADCAST = auto()
    DISCARDED = auto()


TRecovery = TypeVar("TRecovery", bound=BaseException)


@dataclass(slots=True)
class OperationLifecycle:
    """Own common lock, cleanup and recovery bookkeeping for an operation."""

    phase: LifecyclePhase = LifecyclePhase.PRESTART
    release_locks: bool = True
    cleanup_errors: list[Exception] = field(default_factory=list)

    def transition(
        self, phase: LifecyclePhase, *, release_locks: bool | None = None
    ) -> None:
        allowed = {
            LifecyclePhase.PRESTART: {LifecyclePhase.LOCKED},
            LifecyclePhase.LOCKED: {
                LifecyclePhase.LOCKED,
                LifecyclePhase.STARTED,
                LifecyclePhase.WITHHELD,
                LifecyclePhase.BROADCAST,
            },
            LifecyclePhase.STARTED: {
                LifecyclePhase.LOCKED,
                LifecyclePhase.STARTED,
                LifecyclePhase.WITHHELD,
                LifecyclePhase.UPDATED,
                LifecyclePhase.SIGNED,
                LifecyclePhase.BROADCAST,
            },
            LifecyclePhase.WITHHELD: {
                LifecyclePhase.LOCKED,
                LifecyclePhase.BROADCAST,
            },
            LifecyclePhase.UPDATED: {
                LifecyclePhase.STARTED,
                LifecyclePhase.SIGNED,
            },
            LifecyclePhase.SIGNED: {LifecyclePhase.SIGNED, LifecyclePhase.BROADCAST},
            LifecyclePhase.PREPARED: {
                LifecyclePhase.PREPARED,
                LifecyclePhase.BROADCAST,
                LifecyclePhase.DISCARDED,
            },
            LifecyclePhase.BROADCAST: {LifecyclePhase.BROADCAST},
            LifecyclePhase.DISCARDED: {LifecyclePhase.DISCARDED},
        }
        if phase not in allowed[self.phase]:
            raise ValueError(f"Invalid lifecycle transition: {self.phase} -> {phase}")
        self.phase = phase
        if release_locks is not None:
            self.release_locks = release_locks

    async def cleanup(
        self,
        *,
        locked: Iterable[ClassifiedUTXO],
        adapter: JoinMarketAdapter,
        close_message: str,
        unlock_message: str,
    ) -> None:
        """Release operation-owned resources, retaining every cleanup error."""
        if self.release_locks:
            for coin in locked:
                try:
                    adapter.unlock(coin)
                except Exception as exc:
                    self.cleanup_errors.append(exc)
                    logger.error(
                        "{} {}:{} after operation phase {}: {}",
                        unlock_message,
                        coin.utxo.txid,
                        coin.utxo.vout,
                        self.phase,
                        exc,
                    )

        try:
            await adapter.close()
        except Exception as exc:
            self.cleanup_errors.append(exc)
            logger.error(
                "{} after operation phase {}: {}",
                close_message,
                self.phase,
                exc,
            )

    def resolve_if_clean(
        self,
        journal: RecoveryJournal,
        recovery_id: str,
        *,
        terminal_phase: LifecyclePhase,
    ) -> None:
        if not self.cleanup_errors and (
            self.release_locks or self.phase is terminal_phase
        ):
            journal.resolve(recovery_id)

    def raise_recovery_if_needed(
        self,
        operation_error: Exception | None,
        recovery_type: type[TRecovery],
        factory: Callable[[Exception], TRecovery],
    ) -> None:
        """Escalate cleanup failure even when the operation already failed."""
        if not self.cleanup_errors or operation_error is None:
            return
        if isinstance(operation_error, recovery_type):
            return
        raise factory(operation_error) from operation_error
