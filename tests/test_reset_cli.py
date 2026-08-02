from __future__ import annotations

import argparse

import pytest

from docket.config import settings
from docket.main import reset_cli


def test_reset_requires_explicit_yes_and_removes_database_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_name", "brain.db")
    db_path = settings.db_path
    for suffix in ("", "-wal", "-shm", "-journal"):
        db_path.with_name(db_path.name + suffix).write_text("data")

    with pytest.raises(SystemExit) as exc:
        reset_cli(argparse.Namespace(yes=False))
    assert exc.value.code == 2
    assert db_path.exists()

    reset_cli(argparse.Namespace(yes=True))
    assert not any(
        db_path.with_name(db_path.name + suffix).exists()
        for suffix in ("", "-wal", "-shm", "-journal")
    )
