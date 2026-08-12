"""Small synchronous SDK for the Toda REST API."""

from __future__ import annotations

from typing import Any

import httpx


class _Resource:
    def __init__(self, client: "CompanyBrain") -> None:
        self._client = client


class DocumentsClient(_Resource):
    def add(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/documents", payload)

    def update(self, external_id: str, **payload: Any) -> dict[str, Any]:
        payload["external_id"] = external_id
        return self._client._request("PUT", f"/v1/documents/{external_id}", payload)

    def status(self, source_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"/v1/documents/{source_id}/status")


class SearchClient(_Resource):
    def query(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/search", payload)


class ContextClient(_Resource):
    def prepare(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/context:prepare", payload)

    def relationship(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/relationships:query", payload)

    def project(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/projects:context", payload)

    def resolve_owner(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/owners:resolve", payload)


class EventsClient(_Resource):
    def publish(self, **event: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/events", event)

    def publish_batch(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._client._request("POST", "/v1/events:batch", events)


class SkillsClient(_Resource):
    def search(self, organization_id: str, query: str, limit: int = 20) -> dict[str, Any]:
        return self._client._request("POST", "/v1/skills:search", {"organization_id": organization_id, "query": query, "limit": limit})

    def prepare(self, skill_id: str, context_warnings: list[str] | None = None) -> dict[str, Any]:
        return self._client._request("POST", "/v1/skills:prepare", {"skill_id": skill_id, "context_warnings": context_warnings or []})


class ProactiveClient(_Resource):
    def evaluate(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/proactive:evaluate", payload)


class SignalsClient(_Resource):
    def list(self, organization_id: str, state: str | None = None) -> dict[str, Any]:
        params = {"organization_id": organization_id}
        if state:
            params["state"] = state
        return self._client._request("GET", "/v1/signals", params=params)

    def acknowledge(self, signal_id: str, acting_principal_id: str) -> dict[str, Any]:
        return self._client._request("POST", f"/v1/signals/{signal_id}:acknowledge", {"acting_principal_id": acting_principal_id})


class TasksClient(_Resource):
    """Session Manager Tasks and their durable execution traces."""

    def invoke(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/tasks/invoke", payload)

    def get(self, task_id: str, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "GET", f"/v1/tasks/{task_id}", params={"organization_id": organization_id}
        )

    def list(self, organization_id: str, **filters: Any) -> dict[str, Any]:
        return self._client._request(
            "GET", "/v1/tasks", params={"organization_id": organization_id, **filters}
        )

    def cancel(self, task_id: str, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "POST", f"/v1/tasks/{task_id}:cancel", params={"organization_id": organization_id}
        )

    def retry(self, task_id: str, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "POST", f"/v1/tasks/{task_id}:retry", params={"organization_id": organization_id}
        )


class ApprovalsClient(_Resource):
    def list(
        self, organization_id: str, *, status: str | None = None
    ) -> dict[str, Any]:
        params = {"organization_id": organization_id}
        if status:
            params["status"] = status
        return self._client._request("GET", "/v1/approvals", params=params)

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        edited_arguments: dict[str, Any] | None = None,
        run_now: bool = False,
    ) -> dict[str, Any]:
        return self._client._request(
            "POST",
            f"/v1/approvals/{approval_id}:decide",
            {
                "approved": approved,
                "decided_by": decided_by,
                "edited_arguments": edited_arguments,
                "run_now": run_now,
            },
        )


class AutomationsClient(_Resource):
    def create(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/automations", payload)

    def list(self, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "GET",
            "/v1/automations",
            params={"organization_id": organization_id},
        )

    def enable(self, automation_id: str) -> dict[str, Any]:
        return self._client._request(
            "POST", f"/v1/automations/{automation_id}:enable"
        )

    def disable(self, automation_id: str) -> dict[str, Any]:
        return self._client._request(
            "POST", f"/v1/automations/{automation_id}:disable"
        )


class HarnessClient(_Resource):
    def status(self) -> dict[str, Any]:
        return self._client._request("GET", "/v1/harness/status")

    def jobs(
        self, organization_id: str, *, status: str | None = None
    ) -> dict[str, Any]:
        params = {"organization_id": organization_id}
        if status:
            params["status"] = status
        return self._client._request("GET", "/v1/harness/jobs", params=params)

    def drain(self, max_cycles: int = 100) -> dict[str, Any]:
        return self._client._request(
            "POST",
            "/v1/harness/worker:drain",
            params={"max_cycles": max_cycles},
        )

    def feedback(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/harness/feedback", payload)

    def evaluation(self, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "GET",
            "/v1/harness/evaluation",
            params={"organization_id": organization_id},
        )


class RealityClient(_Resource):
    """Canonical objects, temporal facts, cases, policies, and artifacts."""

    def backfill(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/connectors/backfill", payload)

    def sync_runs(self, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "GET",
            "/v1/connectors/sync-runs",
            params={"organization_id": organization_id},
        )

    def link_identifier(self, **payload: Any) -> dict[str, Any]:
        return self._client._request(
            "POST", "/v1/entities/identifiers", payload
        )

    def resolution_candidates(
        self, organization_id: str, status: str | None = None
    ) -> dict[str, Any]:
        params = {"organization_id": organization_id}
        if status:
            params["status"] = status
        return self._client._request(
            "GET", "/v1/entity-resolution/candidates", params=params
        )

    def assert_fact(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/facts", payload)

    def facts(self, **params: Any) -> dict[str, Any]:
        return self._client._request("GET", "/v1/facts", params=params)

    def conflicts(
        self,
        organization_id: str,
        acting_principal_id: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "organization_id": organization_id,
            "acting_principal_id": acting_principal_id,
        }
        if status:
            params["status"] = status
        return self._client._request(
            "GET", "/v1/fact-conflicts", params=params
        )

    def cases(
        self,
        organization_id: str,
        acting_principal_id: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "organization_id": organization_id,
            "acting_principal_id": acting_principal_id,
        }
        if status:
            params["status"] = status
        return self._client._request("GET", "/v1/cases", params=params)

    def judgments(
        self, organization_id: str, acting_principal_id: str
    ) -> dict[str, Any]:
        return self._client._request(
            "GET",
            "/v1/judgments",
            params={
                "organization_id": organization_id,
                "acting_principal_id": acting_principal_id,
            },
        )

    def create_policy(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/policies", payload)

    def policies(self, organization_id: str) -> dict[str, Any]:
        return self._client._request(
            "GET",
            "/v1/policies",
            params={"organization_id": organization_id},
        )

    def create_artifact(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/v1/artifacts", payload)

    def artifacts(self, **params: Any) -> dict[str, Any]:
        return self._client._request("GET", "/v1/artifacts", params=params)


class CompanyBrain:
    """Typed-by-convention SDK surface for harnesses and internal applications."""

    def __init__(self, api_key: str | None = None, base_url: str = "http://127.0.0.1:8080", *, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=30.0, transport=transport)
        self.documents = DocumentsClient(self)
        self.search = SearchClient(self)
        self.context = ContextClient(self)
        self.events = EventsClient(self)
        self.skills = SkillsClient(self)
        self.tasks = TasksClient(self)
        self.approvals = ApprovalsClient(self)
        self.automations = AutomationsClient(self)
        self.harness = HarnessClient(self)
        self.proactive = ProactiveClient(self)
        self.signals = SignalsClient(self)
        self.reality = RealityClient(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CompanyBrain":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, payload: Any | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._http.request(method, path, json=payload, params=params)
        response.raise_for_status()
        return response.json()
