"""Embedding providers used by SQLite-backed hybrid retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from typing import Protocol

import httpx
from opentelemetry.trace import SpanKind

from toda.config import Settings, settings as _loaded_settings
from toda.providers import ProviderResolver


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class InstrumentedEmbeddingProvider:
    """Metadata-only OTel wrapper for embedding calls."""

    def __init__(self, provider: EmbeddingProvider, *, telemetry: object) -> None:
        self.provider = provider
        self.telemetry = telemetry
        self.model = getattr(provider, "model", "unknown")
        self.provider_id = str(
            getattr(provider, "provider_id", None)
            or type(provider).__name__.removesuffix("EmbeddingProvider").lower()
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self.telemetry.span(
            "model.embed",
            component="model",
            kind=SpanKind.CLIENT,
            attributes={
                "toda.provider.id": self.provider_id,
                "gen_ai.request.model": self.model,
                "toda.input.count": len(texts),
                "toda.input.chars": sum(len(text) for text in texts),
                "toda.input.sha256": hashlib.sha256(
                    json.dumps(texts, sort_keys=True).encode()
                ).hexdigest(),
            },
        ) as span:
            try:
                vectors = self.provider.embed(texts)
                span.output({"count": len(vectors), "dimensions": len(vectors[0]) if vectors else 0})
                return vectors
            except Exception as exc:
                span.error(exc)
                raise


def instrument_embedding(provider: EmbeddingProvider, *, telemetry: object) -> EmbeddingProvider:
    if isinstance(provider, InstrumentedEmbeddingProvider):
        return provider
    return InstrumentedEmbeddingProvider(provider, telemetry=telemetry)


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
        # One Slack turn used to embed the same query four times (memory,
        # episodes, evidence, judgments) — each a round-trip to a large
        # remote model. Cache single-text queries only; ingest batches are
        # unique and would just pin RAM.
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._query_cache_max = 64

    def embed(self, texts: list[str]) -> list[list[float]]:
        if len(texts) == 1:
            cached = self._query_cache.get(texts[0])
            if cached is not None:
                self._query_cache.move_to_end(texts[0])
                return [list(cached)]
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
        if len(texts) == 1:
            self._query_cache[texts[0]] = list(vectors[0])
            self._query_cache.move_to_end(texts[0])
            while len(self._query_cache) > self._query_cache_max:
                self._query_cache.popitem(last=False)
        return vectors


DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


class LocalOnnxEmbeddingProvider:
    """Tiny on-device embedder. Query latency is milliseconds, not tens of seconds.

    Uses fastembed (ONNX Runtime). The first call downloads the model into
    the fastembed cache; after that nothing leaves the machine.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or DEFAULT_LOCAL_EMBEDDING_MODEL
        self.provider_id = "local"
        self._embedder: object | None = None
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._query_cache_max = 64

    def _load(self) -> object:
        if self._embedder is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise RuntimeError(
                    "Local embeddings need the fastembed package. "
                    "Install it with: python -m pip install fastembed"
                ) from exc
            self._embedder = TextEmbedding(model_name=self.model)
        return self._embedder

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) == 1:
            cached = self._query_cache.get(texts[0])
            if cached is not None:
                self._query_cache.move_to_end(texts[0])
                return [list(cached)]
        raw = list(self._load().embed(texts))  # type: ignore[union-attr]
        vectors = [list(map(float, item)) for item in raw]
        if len(vectors) != len(texts):
            raise RuntimeError("local embedding provider returned an incomplete response")
        if len(texts) == 1:
            self._query_cache[texts[0]] = list(vectors[0])
            self._query_cache.move_to_end(texts[0])
            while len(self._query_cache) > self._query_cache_max:
                self._query_cache.popitem(last=False)
        return vectors


def build_embedding_provider(config: Settings | None = None) -> EmbeddingProvider:
    config = config or _loaded_settings
    resolved = ProviderResolver(
        config.models.providers, config.models.assignments
    ).resolve("embedding")
    if resolved:
        if resolved.connection.provider == "local":
            model = resolved.assignment.model
            if not model or model in {"auto", "local"}:
                model = DEFAULT_LOCAL_EMBEDDING_MODEL
            return LocalOnnxEmbeddingProvider(model)
        if resolved.profile.api_mode != "chat_completions":
            raise ValueError(
                f"Provider {resolved.profile.name!r} does not expose OpenAI-compatible embeddings"
            )
        provider = OpenAICompatibleEmbeddingProvider(
            resolved.connection.api_key or "local",
            resolved.assignment.model,
            resolved.profile.endpoint(resolved.connection),
            timeout_seconds=config.models.timeout_seconds,
        )
        provider.provider_id = resolved.connection.provider
        return provider
    return DeterministicEmbeddingProvider(config.models.embedding_dimensions)
