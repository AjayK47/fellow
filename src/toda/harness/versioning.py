"""Deterministic, credential-free run configuration fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_SECRET_PARTS = ("api_key", "apikey", "authorization", "credential", "password", "secret", "token")


def _without_secrets(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in _SECRET_PARTS):
        return "[omitted]"
    if isinstance(value, dict):
        return {str(k): _without_secrets(v, str(k)) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_without_secrets(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def stable_hash(value: Any) -> str:
    payload = json.dumps(_without_secrets(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_configuration_versions(
    *,
    prompts: Any = None,
    tools: Any = None,
    skills: Any = None,
    policy: Any = None,
    models: Any = None,
    catalog: Any = None,
    build: Any = "0.1.0",
) -> dict[str, str]:
    return {
        "prompt_hash": stable_hash(prompts),
        "toolset_hash": stable_hash(tools),
        "skillset_hash": stable_hash(skills),
        "policy_hash": stable_hash(policy),
        "model_hash": stable_hash(models),
        "catalog_hash": stable_hash(catalog),
        "build_hash": stable_hash(build),
    }
