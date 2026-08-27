from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.comparison import summarize_trials
from evals.scenarios import SCENARIOS, scenario
from toda.harness.model_client import ScriptedModelClient, instrument_model
from toda.harness.models import ModelResponse
from toda.harness.versioning import stable_hash
from toda.services.models_catalog import ModelsCatalogService
from toda.services.readiness import readiness_report
from toda.telemetry.metrics import TelemetryMetrics
from toda.telemetry.validation import validate_trace
from toda.storage.db import BrainStore


def test_models_dev_shape_is_parsed_without_losing_identity_or_pricing(tmp_path: Path):
    service = ModelsCatalogService(cache_path=tmp_path / "catalog.json", enabled=False)
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "models.dev.catalog.json").read_text()
    )
    models, metadata = service._parse(fixture, source="remote")
    service._models = models
    assert metadata["model_count"] == 2
    assert metadata["provider_model_count"] == 2
    openai = service.find("openai", "gpt-4o")
    assert openai is not None
    assert openai["context_length"] == 128000
    assert openai["max_output_tokens"] == 16384
    assert openai["input_usd_per_1m"] == pytest.approx(0.0025)
    assert openai["output_usd_per_1m"] == pytest.approx(10.0)
    assert openai["capabilities"] == ["structured_output", "tool_calling", "vision"]


def test_versions_omit_credentials_and_metric_labels_remain_low_cardinality():
    assert stable_hash({"model": "x", "api_key": "first"}) == stable_hash(
        {"model": "x", "api_key": "second"}
    )
    metrics = TelemetryMetrics()
    metrics.counter("test_requests_total", labels={"provider": "openai"})
    with pytest.raises(ValueError):
        metrics.counter("test_requests_total", labels={"task_id": "task-secret"})
    metrics.shutdown()


def test_model_instrumentation_is_exactly_one_safe_span_with_estimated_cost(tmp_path: Path):
    store = BrainStore(tmp_path / "telemetry.db")

    class Catalog:
        def estimate_cost(self, **_: object) -> dict[str, object]:
            return {"usd": 0.0125, "source": "bundled_catalog"}

        def status(self) -> dict[str, str]:
            return {"content_hash": "catalog-hash", "fetched_at": "2026-08-26T00:00:00+00:00"}

    client = instrument_model(
        ScriptedModelClient(
            [
                ModelResponse(
                    content="safe answer",
                    usage={"input_tokens": 100, "output_tokens": 25},
                )
            ]
        ),
        telemetry=store.telemetry,
        role="main",
        catalog=Catalog(),
    )
    with store.telemetry.span("agent.invoke", organization_id="org_acme") as root:
        response = client.generate(messages=[{"role": "user", "content": "secret prompt"}])
        trace_id = f"{root.span.get_span_context().trace_id:032x}"
    rows = store.list_otel_spans(trace_id, organization_id="org_acme")
    model_rows = [row for row in rows if row["name"] == "model.generate"]
    assert response.usage["cost_state"] == "estimated"
    assert len(model_rows) == 1
    assert model_rows[0]["attributes"]["toda.cost.state"] == "estimated"
    assert model_rows[0]["attributes"]["gen_ai.usage.input_tokens"] == 100
    assert "secret prompt" not in str(model_rows[0])
    store.close()


def test_completeness_fails_deterministically_when_required_coverage_is_missing():
    incomplete = validate_trace([], delivery_required=True)
    assert incomplete["complete"] is False
    assert "agent.invoke" in incomplete["missing"]
    assert "delivery.send" in incomplete["missing"]
    complete = validate_trace(
        [
            {
                "name": "agent.invoke", "end_time_ns": 2, "span_id": "a",
                "attributes": {
                    "toda.prompt.hash": "p", "toda.toolset.hash": "t",
                    "toda.skillset.hash": "s", "toda.policy.hash": "y",
                    "toda.catalog.version": "c",
                },
            },
            {
                "name": "model.generate",
                "end_time_ns": 2,
                "span_id": "m",
                "attributes": {
                    "toda.provider.id": "openai",
                    "toda.provider_model.id": "openai/gpt-test",
                    "toda.cost.state": "unavailable",
                },
            },
            {"name": "delivery.send", "end_time_ns": 2, "span_id": "d", "attributes": {}},
        ],
        delivery_required=True,
    )
    assert complete["complete"] is True


def test_readiness_is_machine_readable_and_does_not_call_a_provider_without_assignments(empty_brain):
    report = readiness_report(empty_brain)
    checks = {item["name"]: item for item in report["checks"]}
    assert report["mode"] == "blocked"
    assert checks["provider_probe"]["status"] == "fail"
    assert checks["payload_capture_disabled"]["status"] == "pass"
    assert checks["execution_budgets"]["status"] == "pass"


def test_scenario_pack_and_comparison_make_privacy_failures_fatal():
    assert len(SCENARIOS) >= 20
    assert scenario("approval.resume").expected_approval == "required"
    report = summarize_trials(
        [
            {
                "candidate": "a",
                "scenario": "question.simple",
                "scenario_version": 1,
                "outcome": "completed",
                "privacy": {"violation": True},
                "completeness": {"complete": True},
                "metrics": {"wall_duration_ms": 10, "tokens": {"total_tokens": 5}},
            }
        ]
    )
    assert report["comparison_is_valid"] is False
    assert report["fatal_trials"] == 1


def test_pilot_gate_blocks_and_audits_task_start(empty_brain):
    from toda.services.readiness import ReadinessBlockedError, enforce_real_world_readiness

    empty_brain.settings = empty_brain.settings.model_copy(
        update={"agent": empty_brain.settings.agent.model_copy(update={"require_real_world_readiness": True})}
    )

    with pytest.raises(ReadinessBlockedError) as exc_info:
        enforce_real_world_readiness(empty_brain, principal_id="user_1", trigger_kind="manual")

    assert "real-world readiness" in str(exc_info.value)

    # Check audit log for refusal
    audit_rows = empty_brain.store.list_audit(empty_brain.settings.organization.id, "task.start_blocked_readiness")
    assert len(audit_rows) >= 1
    assert audit_rows[0]["principal_id"] == "user_1"


def test_api_models_endpoint_requires_connection(api_client, empty_brain):
    # Missing connection_name parameter fails with 422
    resp = api_client.get("/v1/models")
    assert resp.status_code == 422

    # Unknown connection returns 404
    resp = api_client.get("/v1/models", params={"connection_name": "unknown_conn"})
    assert resp.status_code == 404
    assert "not configured" in resp.json()["detail"]

    # Invalid role returns 400
    # Register dummy connection
    from toda.providers import ProviderConnection
    empty_brain.settings.models.providers["test_conn"] = ProviderConnection(provider="openrouter", api_key="sk-test")
    resp = api_client.get("/v1/models", params={"connection_name": "test_conn", "role": "invalid_role"})
    assert resp.status_code == 400
    assert "Invalid role" in resp.json()["detail"]

    # Valid query returns selectable models with explainable ranking
    resp = api_client.get("/v1/models", params={"connection_name": "test_conn", "role": "main", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert data["connection_name"] == "test_conn"
    assert data["provider"] == "openrouter"


def test_admin_catalog_route(api_client):
    resp = api_client.get("/v1/admin/catalog/models", params={"limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert "catalog" in data
    assert data["count"] <= 5


def test_connector_health_records_are_tenant_scoped_and_fresh(empty_brain):
    record = empty_brain.store.record_connector_health(
        empty_brain.settings.organization.id,
        "composio",
        status="ok",
        detail={"account_count": 2, "api_key": "must-not-persist"},
    )
    rows = empty_brain.store.list_harness_records(
        "connector_health",
        empty_brain.settings.organization.id,
        where={"connector": "composio"},
    )
    assert rows == [record]
    assert rows[0]["detail"] == {"account_count": 2}


def test_model_assignment_route_persists_validated_config(
    api_client, empty_brain, monkeypatch, tmp_path: Path
):
    from toda.providers import ProviderConnection

    empty_brain.settings.models.providers["primary"] = ProviderConnection(
        provider="openrouter", api_key="secret"
    )
    monkeypatch.setattr(
        "toda.api.routes.providers.models",
        lambda *_args, **_kwargs: [{"id": "vendor/model"}],
    )
    destination = tmp_path / "config.json"
    monkeypatch.setattr(
        "toda.api.routes.save_settings",
        lambda _settings: destination,
    )

    response = api_client.put(
        "/v1/model-settings/assignment",
        json={"role": "main", "connection_name": "primary", "model": "vendor/model"},
    )
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert response.json()["assignment"] == {
        "connection": "primary",
        "model": "vendor/model",
    }
