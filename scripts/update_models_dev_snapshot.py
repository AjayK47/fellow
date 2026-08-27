#!/usr/bin/env python3
"""Update the bundled models.dev catalog snapshot and provenance metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

DEFAULT_CATALOG_URL = "https://models.dev/catalog.json"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "src" / "toda" / "data"


def update_snapshot(
    catalog_url: str = DEFAULT_CATALOG_URL,
    output_dir: Path | str = DEFAULT_DATA_DIR,
    timeout: float = 30.0,
) -> int:
    output_path = Path(output_dir)
    snapshot_path = output_path / "models_catalog.json"
    provenance_path = output_path / "models_catalog.provenance.json"

    print(f"Fetching models.dev catalog from {catalog_url}...", file=sys.stderr)
    try:
        response = httpx.get(catalog_url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"Error fetching catalog: {exc}", file=sys.stderr)
        return 1

    from toda.services.models_catalog import ModelsCatalogService, PARSER_VERSION

    service = ModelsCatalogService(cache_path=output_path / ".tmp_check.json", enabled=False)
    models, metadata = service._parse(payload, source="remote")

    if not models or metadata.get("provider_model_count", 0) == 0:
        print("Error: Parsed catalog contained no provider-model records.", file=sys.stderr)
        return 2

    output_path.mkdir(parents=True, exist_ok=True)

    tmp_snapshot: Path | None = None
    tmp_prov: Path | None = None
    try:
        # Write snapshot atomically
        payload_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=output_path, delete=False, suffix=".tmp") as tmp:
            tmp.write(payload_bytes)
            tmp_snapshot = Path(tmp.name)

        content_hash = hashlib.sha256(payload_bytes).hexdigest()
        provenance = {
            "upstream_url": catalog_url,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "raw_content_hash": content_hash,
            "normalized_projection_hash": metadata.get("normalized_projection_hash"),
            "content_hash": content_hash,
            "parser_version": PARSER_VERSION,
            "provider_count": metadata.get("provider_count", len(payload.get("providers", {}))),
            "model_count": metadata.get("model_count", len(models)),
            "provider_model_count": metadata.get("provider_model_count", 0),
        }

        prov_bytes = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=output_path, delete=False, suffix=".tmp") as tmp:
            tmp.write(prov_bytes)
            tmp_prov = Path(tmp.name)

        tmp_snapshot.replace(snapshot_path)
        tmp_snapshot = None
        tmp_prov.replace(provenance_path)
        tmp_prov = None

        print(
            f"Updated {snapshot_path} ({metadata.get('model_count')} models, "
            f"{metadata.get('provider_model_count')} provider-models).",
            file=sys.stderr,
        )
        return 0
    finally:
        if tmp_snapshot and tmp_snapshot.exists():
            try:
                os.unlink(tmp_snapshot)
            except OSError:
                pass
        if tmp_prov and tmp_prov.exists():
            try:
                os.unlink(tmp_prov)
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Update models.dev catalog snapshot")
    parser.add_argument("--url", default=DEFAULT_CATALOG_URL, help="Catalog URL")
    parser.add_argument("--output-dir", default=str(DEFAULT_DATA_DIR), help="Output directory")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout")
    args = parser.parse_args()

    return update_snapshot(catalog_url=args.url, output_dir=args.output_dir, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
