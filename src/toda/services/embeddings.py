"""Embedding providers used by SQLite-backed hybrid retrieval."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from toda.config import Settings, settings as _loaded_settings
from toda.providers import ProviderResolver


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbeddingProvider:
    """Offline, stable embedding fallback for tests and credential-free setups."""

    model = "deterministic-hash-v1"

    def __init__(self, dimensions: int = 96) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r"[a-z0-9_]+", text.lower()):
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[bucket] += sign
            norm = math.sqrt(sum(v * v for v in vector))
            vectors.append([v / norm for v in vector] if norm else vector)
        return vectors


class OpenAICompatibleEmbeddingProvider:
    """Embeddings client resolved through a provider connection."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Honours models.timeout_seconds like every model client does. This
        # was hardcoded to 30s, so a configured 600s did nothing for
        # embeddings and a large embedding model (qwen3-embedding-8b, say)
        # timed out mid-ingest on a batch the provider was still working on.
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [list(item["embedding"]) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding provider returned an incomplete response")
        return vectors


def build_embedding_provider(config: Settings | None = None) -> EmbeddingProvider:
    config = config or _loaded_settings
    resolved = ProviderResolver(
        config.models.providers, config.models.assignments
    ).resolve("embedding")
    if resolved:
        if resolved.profile.api_mode != "chat_completions":
            raise ValueError(
                f"Provider {resolved.profile.name!r} does not expose OpenAI-compatible embeddings"
            )
        return OpenAICompatibleEmbeddingProvider(
            resolved.connection.api_key or "local",
            resolved.assignment.model,
            resolved.profile.endpoint(resolved.connection),
            timeout_seconds=config.models.timeout_seconds,
        )
    return DeterministicEmbeddingProvider(config.models.embedding_dimensions)
