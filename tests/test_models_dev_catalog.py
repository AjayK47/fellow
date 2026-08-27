from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from toda.harness.model_client import normalize_usage
from toda.providers import ProviderProfile
from toda.services.models_catalog import PARSER_VERSION, ModelsCatalogService


def test_bundled_snapshot_matches_provenance():
    data_dir = Path(__file__).resolve().parents[1] / "src" / "toda" / "data"
    snapshot_path = data_dir / "models_catalog.json"
    provenance_path = data_dir / "models_catalog.provenance.json"

    assert snapshot_path.exists()
    assert provenance_path.exists()

    snapshot_bytes = snapshot_path.read_bytes()
    expected_hash = hashlib.sha256(snapshot_bytes).hexdigest()

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["content_hash"] == expected_hash
    assert provenance["parser_version"] == PARSER_VERSION
    assert provenance["provider_count"] > 0
    assert provenance["model_count"] > 0


def test_corrupt_cache_is_quarantined(tmp_path: Path):
    cache_path = tmp_path / "models_catalog.json"
    meta_path = tmp_path / "models_catalog.json.meta.json"

    cache_path.write_text("{ corrupt json ...", encoding="utf-8")
    meta_path.write_text('{"valid": true}', encoding="utf-8")

    service = ModelsCatalogService(cache_path=cache_path, enabled=False)

    quarantined_files = list(tmp_path.glob("models_catalog.json.corrupt-*"))
    assert len(quarantined_files) == 1
    assert len(service.models()) > 0
    assert service.status()["fallback"] is True


def test_semantically_corrupt_empty_cache_is_quarantined(tmp_path: Path):
    cache_path = tmp_path / "models_catalog.json"
    meta_path = tmp_path / "models_catalog.json.meta.json"

    # Valid JSON but 0 provider models
    cache_path.write_text('{"providers": {}}', encoding="utf-8")
    meta_path.write_text('{"valid": true}', encoding="utf-8")

    service = ModelsCatalogService(cache_path=cache_path, enabled=False)

    quarantined_files = list(tmp_path.glob("models_catalog.json.corrupt-*"))
    assert len(quarantined_files) == 1
    assert len(service.models()) > 0
    assert service.status()["fallback"] is True


def test_failure_backoff_and_retry_after(tmp_path: Path, monkeypatch):
    cache_path = tmp_path / "models_catalog.json"
    service = ModelsCatalogService(cache_path=cache_path, enabled=True, refresh_interval_seconds=86400)

    def mock_get(*args, **kwargs):
        raise ConnectionError("Network down")

    monkeypatch.setattr("toda.services.models_catalog.httpx.get", mock_get)

    status = service.refresh(force=True)
    assert status["last_refresh_result"] == "failed_using_fallback"
    assert "Network down" in status["last_error"]
    assert service._retry_after_mono > time.monotonic()

    # _is_stale() correctly reports True because data is on fallback
    assert service._is_stale() is True

    # Non-forced refresh should be suppressed while retry_after is active
    called = False
    def mock_get_check(*args, **kwargs):
        nonlocal called
        called = True
        raise ConnectionError("Should not be called")

    monkeypatch.setattr("toda.services.models_catalog.httpx.get", mock_get_check)
    service.refresh(force=False)
    assert called is False, "Non-forced refresh must be suppressed during retry_after"


def test_directional_modalities_parsing(tmp_path: Path):
    service = ModelsCatalogService(cache_path=tmp_path / "models_catalog.json", enabled=False)
    payload = {
        "providers": {
            "test_provider": {
                "name": "Test Provider",
                "api_endpoint": "https://api.test.com/v1",
                "doc_url": "https://docs.test.com",
                "env_keys": ["TEST_API_KEY"],
                "models": {
                    "multimodal_model": {
                        "id": "multimodal_model",
                        "name": "Multimodal Pro",
                        "description": "Fast multimodal model",
                        "family": "multimodal",
                        "base_model": "base-v1",
                        "knowledge": "2025-01",
                        "modalities": {"input": ["text", "image", "audio"], "output": ["text", "audio"]},
                        "tool_call": True,
                        "structured_output": True,
                    }
                }
            }
        }
    }
    models, meta = service._parse(payload, source="remote")
    assert len(models) == 1
    m = models[0]
    assert m["name"] == "Multimodal Pro"
    assert m["family"] == "multimodal"
    assert m["base_model"] == "base-v1"
    assert m["knowledge_cutoff"] == "2025-01"
    assert m["input_modalities"] == ["audio", "image", "text"]
    assert m["output_modalities"] == ["audio", "text"]
    assert "vision" in m["capabilities"]
    assert "transcription" in m["capabilities"]
    assert "tts" in m["capabilities"]
    assert "tool_calling" in m["capabilities"]
    assert "structured_output" in m["capabilities"]
    assert m["provider_metadata"]["name"] == "Test Provider"
    assert m["provider_metadata"]["env_keys"] == ["TEST_API_KEY"]


def test_normalize_usage_preserves_cost():
    raw_usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.0042,
        "cost_state": "actual",
    }
    normalized = normalize_usage(raw_usage)
    assert normalized["input_tokens"] == 100
    assert normalized["output_tokens"] == 50
    assert normalized["total_tokens"] == 150
    assert normalized["cost_usd"] == 0.0042
    assert normalized["cost_state"] == "actual"


def test_static_contract_provider_profile_has_no_hardcoded_catalogs():
    from dataclasses import fields
    import toda.providers as prov_mod

    field_names = {f.name for f in fields(ProviderProfile)}
    forbidden_fields = {
        "chat_catalog",
        "embedding_catalog",
        "vision_models",
        "image_catalog",
        "transcription_catalog",
        "tts_catalog",
        "recommended_model",
        "recommended_image_model",
        "recommended_transcription_model",
        "recommended_tts_model",
        "recommended_embedding_model",
        "supports_embeddings",
        "embedding_models",
    }
    present_forbidden = field_names & forbidden_fields
    assert not present_forbidden, f"ProviderProfile contains forbidden hardcoded model fields: {present_forbidden}"

    for profile in prov_mod.providers.list():
        for attr in forbidden_fields:
            assert not hasattr(profile, attr)


def test_models_catalog_304_refresh_preserves_content(tmp_path: Path, monkeypatch):
    import httpx

    cache_path = tmp_path / "models_catalog.json"
    cache_path.write_text('{"providers": {"test": {"models": {"m1": {"id": "m1", "tool_call": true}}}}}', encoding="utf-8")
    service = ModelsCatalogService(cache_path=cache_path, enabled=True)

    class Mock304Response:
        status_code = 304
        headers = {"ETag": '"abc-123"'}

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Mock304Response())

    initial_models_count = len(service.models())
    status = service.refresh(force=True)
    assert status["last_refresh_result"] == "not_modified"
    assert len(service.models()) == initial_models_count
    assert status["last_successful_fetch_at"] is not None


def test_models_catalog_rejects_empty_remote_refresh(tmp_path: Path, monkeypatch):
    import httpx

    cache_path = tmp_path / "models_catalog.json"
    service = ModelsCatalogService(cache_path=cache_path, enabled=True)

    class MockEmptyResponse:
        status_code = 200
        headers = {}
        content = b'{"models": {}, "providers": {}}'

        def raise_for_status(self):
            pass

        def json(self):
            return {"models": {}, "providers": {}}

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: MockEmptyResponse())

    initial_models_count = len(service.models())
    status = service.refresh(force=True)
    assert status["last_refresh_result"] == "failed_using_fallback"
    assert "no provider-model records" in status["last_error"]
    assert len(service.models()) == initial_models_count


def test_snapshot_generator_with_local_http_fixture_server(tmp_path: Path):
    from scripts.update_models_dev_snapshot import update_snapshot

    fixture_payload = {
        "models": {
            "openai/gpt-4o": {
                "id": "openai/gpt-4o",
                "tool_call": True,
                "structured_output": True,
            }
        },
        "providers": {
            "openai": {
                "id": "openai",
                "name": "OpenAI",
                "models": {
                    "gpt-4o": {
                        "id": "gpt-4o",
                        "name": "GPT-4o",
                        "tool_call": True,
                        "structured_output": True,
                        "modalities": {"input": ["text", "image"], "output": ["text"]},
                        "pricing": {"input": 2.5, "output": 10.0},
                    }
                },
            }
        },
    }

    body = json.dumps(fixture_payload).encode("utf-8")

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", '"etag-xyz-123"')
            self.send_header("Last-Modified", "Wed, 27 Aug 2026 00:00:00 GMT")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    out_dir = tmp_path / "catalog_out"
    code = update_snapshot(catalog_url=f"http://{host}:{port}/catalog.json", output_dir=out_dir)
    server.server_close()
    server_thread.join()

    assert code == 0
    snapshot_file = out_dir / "models_catalog.json"
    prov_file = out_dir / "models_catalog.provenance.json"
    assert snapshot_file.exists()
    assert prov_file.exists()

    prov_data = json.loads(prov_file.read_text(encoding="utf-8"))
    assert prov_data["etag"] == '"etag-xyz-123"'
    assert prov_data["last_modified"] == "Wed, 27 Aug 2026 00:00:00 GMT"
    assert prov_data["provider_count"] == 1
    assert prov_data["provider_model_count"] == 1
    assert prov_data["content_hash"] == hashlib.sha256(snapshot_file.read_bytes()).hexdigest()


def test_direct_refresh_is_single_flight(tmp_path: Path, monkeypatch):
    import httpx
    from concurrent.futures import ThreadPoolExecutor

    cache_path = tmp_path / "models_catalog.json"
    cache_path.write_text('{"providers": {"test": {"models": {"m1": {"id": "m1", "tool_call": true}}}}}', encoding="utf-8")
    service = ModelsCatalogService(cache_path=cache_path, enabled=True)

    call_count = 0

    def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        class R:
            status_code = 304
            headers = {}
        return R()

    monkeypatch.setattr(httpx, "get", mock_get)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(service.refresh, force=True) for _ in range(5)]
        results = [f.result() for f in futures]

    assert len(results) == 5
    # Due to true single-flight locking, only 1 actual HTTP request happens and all callers get completed status
    assert call_count == 1
    for r in results:
        assert r["last_refresh_result"] == "not_modified"
        assert r["last_successful_fetch_at"] is not None


def test_refresh_single_flight_is_shared_by_service_instances(tmp_path: Path, monkeypatch):
    import httpx
    from concurrent.futures import ThreadPoolExecutor

    cache_path = tmp_path / "models_catalog.json"
    cache_path.write_text(
        '{"providers": {"test": {"models": {"m1": {"id": "m1", "tool_call": true}}}}}',
        encoding="utf-8",
    )
    services = [
        ModelsCatalogService(cache_path=cache_path, enabled=True),
        ModelsCatalogService(cache_path=cache_path, enabled=True),
    ]
    call_count = 0

    def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)

        class Response:
            status_code = 304
            headers = {}

        return Response()

    monkeypatch.setattr(httpx, "get", mock_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.refresh(force=True), services))

    assert call_count == 1
    assert {result["last_refresh_result"] for result in results} == {"not_modified"}


def test_background_refresh_reserves_flight_before_thread_start(tmp_path: Path, monkeypatch):
    import httpx

    cache_path = tmp_path / "models_catalog.json"
    cache_path.write_text(
        '{"providers": {"test": {"models": {"m1": {"id": "m1", "tool_call": true}}}}}',
        encoding="utf-8",
    )
    service = ModelsCatalogService(cache_path=cache_path, enabled=True)
    call_count = 0
    finished = threading.Event()

    def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        finished.set()

        class Response:
            status_code = 304
            headers = {}

        return Response()

    monkeypatch.setattr(httpx, "get", mock_get)
    for _ in range(10):
        service.refresh_in_background(force=True)

    assert finished.wait(2)
    time.sleep(0.02)
    assert call_count == 1


def test_failed_leader_result_is_visible_to_other_instances(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    cache_path = tmp_path / "models_catalog.json"
    services = [
        ModelsCatalogService(cache_path=cache_path, enabled=True),
        ModelsCatalogService(cache_path=cache_path, enabled=True),
    ]
    call_count = 0

    def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        raise ConnectionError("leader failed")

    monkeypatch.setattr("toda.services.models_catalog.httpx.get", mock_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.refresh(force=True), services))

    assert call_count == 1
    assert {result["last_refresh_result"] for result in results} == {
        "failed_using_fallback"
    }
