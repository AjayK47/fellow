"""Local ONNX embeddings are selected from config without a remote key."""

from __future__ import annotations

import json

from toda.config import load_settings
from toda.services.embeddings import (
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    LocalOnnxEmbeddingProvider,
    build_embedding_provider,
)


def test_local_embedding_assignment_does_not_need_an_api_key(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {"local": {"provider": "local"}},
                    "assignments": {
                        "embedding": {
                            "connection": "local",
                            "model": DEFAULT_LOCAL_EMBEDDING_MODEL,
                        }
                    },
                }
            }
        )
    )
    path.chmod(0o600)
    provider = build_embedding_provider(load_settings(path))
    assert isinstance(provider, LocalOnnxEmbeddingProvider)
    assert provider.model == DEFAULT_LOCAL_EMBEDDING_MODEL


def test_local_embedder_caches_a_repeated_query(monkeypatch):
    calls: list[list[str]] = []

    class FakeEmbedder:
        def embed(self, texts):
            calls.append(list(texts))
            return [[0.1, 0.2, 0.3] for _ in texts]

    provider = LocalOnnxEmbeddingProvider()
    monkeypatch.setattr(provider, "_load", lambda: FakeEmbedder())
    first = provider.embed(["hello"])
    second = provider.embed(["hello"])
    assert first == second
    assert len(calls) == 1
