from __future__ import annotations

import argparse

import toda.services.container as container_module
from toda.config import settings
from toda.main import reindex_cli
from toda.storage.db import BrainStore


def test_reindex_rebuilds_vec_tables_from_stored_embeddings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "data_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "db_name", "brain.db")
    # get_app() caches one TodaApp globally -- give this test a clean
    # slate (and don't leak it forward into other tests).
    monkeypatch.setattr(container_module, "_app", None)

    brain = container_module.get_app(settings.storage.db_path)
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="test",
        source_type="document",
        external_id="reindex-cli-doc",
        content="Reindex CLI coverage document about export pipeline retries.",
        scope_id="scope_org_public",
        learn=False,
        processing_mode="reference",
    )
    source_id = result["source"]["id"]
    with brain.store.cursor() as cur:
        cur.execute("DELETE FROM vec_chunks WHERE id LIKE ?", (f"chunk_{source_id}_%",))
        cur.execute("SELECT COUNT(*) AS n FROM vec_chunks WHERE id LIKE ?", (f"chunk_{source_id}_%",))
        assert cur.fetchone()["n"] == 0

    # reindex_cli closes the app it opens (a one-shot CLI invocation) --
    # reopen the store fresh to verify, rather than reusing the closed one.
    reindex_cli(argparse.Namespace(force=False))

    reopened = BrainStore(settings.storage.db_path)
    try:
        with reopened.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM vec_chunks WHERE id LIKE ?", (f"chunk_{source_id}_%",))
            assert cur.fetchone()["n"] == 1
    finally:
        reopened.close()
