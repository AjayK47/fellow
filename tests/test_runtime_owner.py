from __future__ import annotations

import pytest

from toda.runtime_owner import DatabaseOwner, DatabaseOwnershipError


def test_only_one_daemon_can_own_a_database(tmp_path):
    database = tmp_path / "toda.db"
    first = DatabaseOwner(database)
    second = DatabaseOwner(database)

    first.acquire()
    try:
        with pytest.raises(DatabaseOwnershipError, match="already owns"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    assert second.held is True
    second.release()


def test_database_owner_context_releases_after_failure(tmp_path):
    database = tmp_path / "toda.db"

    with pytest.raises(RuntimeError):
        with DatabaseOwner(database):
            raise RuntimeError("startup failed")

    replacement = DatabaseOwner(database)
    replacement.acquire()
    replacement.release()
