"""Exclusive ownership of a Toda installation's coordinating database."""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout


class DatabaseOwnershipError(RuntimeError):
    """Raised when another Toda daemon already coordinates this database."""


class DatabaseOwner:
    """Hold one process-wide lock for the complete Toda daemon lifetime.

    SQLite still uses several thread-local connections inside that process.
    This lock prevents a second API, worker, or messaging process from
    becoming an independent coordinator for the same installation.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.lock_path = Path(f"{self.db_path}.owner.lock")
        self._lock = FileLock(self.lock_path)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        if self._held:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=0)
        except Timeout as exc:
            raise DatabaseOwnershipError(
                f"Another Toda process already owns {self.db_path}. "
                "Use that daemon's local API or stop it before starting Toda again."
            ) from exc
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        self._lock.release()
        self._held = False

    def __enter__(self) -> DatabaseOwner:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
