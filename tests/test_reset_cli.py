from __future__ import annotations

import argparse

import pytest

from toda.config import settings
from toda.main import reset_cli


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


def test_reset_also_clears_task_workspaces(tmp_path, monkeypatch):
    """Workspaces are real files written by tasks and are NOT recreated
    from the database, so leaving them behind means a "fresh start" isn't
    one. A live install had 4,191 stale workspace directories survive
    every reset, all from an architecture that no longer exists."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_name", "brain.db")
    workspace_root = tmp_path / "workspaces"
    monkeypatch.setattr(settings, "workspace_root", workspace_root)
    settings.db_path.write_text("data")
    stale = workspace_root / "task_old"
    stale.mkdir(parents=True)
    (stale / "report.md").write_text("leftover work")
    loose_file = workspace_root / "loose.txt"
    loose_file.write_text("also leftover")

    reset_cli(argparse.Namespace(yes=True))

    assert workspace_root.exists()  # the root itself stays, just emptied
    assert not stale.exists()
    assert not loose_file.exists()
    assert list(workspace_root.iterdir()) == []


def test_reset_refuses_a_workspace_root_that_is_not_toda_owned(tmp_path, monkeypatch):
    """Guard against a misconfigured workspace_root (pointed at a home
    directory, say) turning a reset into an unrecoverable wipe."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_name", "brain.db")
    (tmp_path / "data").mkdir()
    settings.db_path.write_text("data")
    unrelated = tmp_path / "my_documents"
    unrelated.mkdir()
    precious = unrelated / "taxes.pdf"
    precious.write_text("do not delete")
    monkeypatch.setattr(settings, "workspace_root", unrelated)

    reset_cli(argparse.Namespace(yes=True))

    assert precious.exists()
