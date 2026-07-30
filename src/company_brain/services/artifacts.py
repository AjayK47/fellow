"""Durable, permission-scoped outputs produced by company-agent runs."""

from __future__ import annotations

import json
from typing import Any

from company_brain.domain.models import new_id, utcnow


class ArtifactService:
    def __init__(self, store: Any, audit: Any) -> None:
        self.store = store
        self.audit = audit

    def create(
        self,
        *,
        organization_id: str,
        artifact_type: str,
        title: str,
        content: Any,
        scope_id: str,
        run_id: str | None = None,
        created_by: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = self.store.get_scope(scope_id)
        if not scope or scope["organization_id"] != organization_id:
            raise ValueError("artifact scope does not belong to organization")
        now = utcnow().isoformat()
        artifact = {
            "id": new_id("artifact"),
            "organization_id": organization_id,
            "run_id": run_id,
            "artifact_type": artifact_type,
            "title": title,
            "status": "active",
            "scope_id": scope_id,
            "content": content,
            "created_by": created_by,
            "metadata": metadata or {},
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        self._write(artifact)
        self.audit.record(
            organization_id,
            "artifact.created",
            {
                "artifact_id": artifact["id"],
                "artifact_type": artifact_type,
                "run_id": run_id,
            },
            principal_id=created_by,
        )
        return artifact

    def update(
        self,
        artifact_id: str,
        *,
        content: Any,
        updated_by: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        artifact = self.get(artifact_id)
        if not artifact:
            raise KeyError(artifact_id)
        artifact["content"] = content
        artifact["title"] = title or artifact["title"]
        artifact["version"] = int(artifact.get("version", 1)) + 1
        artifact["updated_by"] = updated_by
        artifact["updated_at"] = utcnow().isoformat()
        self._write(artifact)
        return artifact

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        with self.store.cursor() as cur:
            cur.execute("SELECT payload FROM artifacts WHERE id = ?", (artifact_id,))
            row = cur.fetchone()
        return json.loads(row["payload"]) if row else None

    def list(
        self,
        organization_id: str,
        *,
        scope_ids: list[str] | None = None,
        artifact_type: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?", "status = 'active'"]
        params: list[Any] = [organization_id]
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if scope_ids is not None:
            if not scope_ids:
                return []
            clauses.append(f"scope_id IN ({','.join('?' for _ in scope_ids)})")
            params.extend(scope_ids)
        with self.store.cursor() as cur:
            cur.execute(
                f"""SELECT payload FROM artifacts
                WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC""",
                params,
            )
            return [json.loads(row["payload"]) for row in cur.fetchall()]

    def _write(self, artifact: dict[str, Any]) -> None:
        payload = json.dumps(artifact, default=str)
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO artifacts
                (id, organization_id, run_id, artifact_type, title, status,
                 scope_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                    status=excluded.status, payload=excluded.payload,
                    updated_at=excluded.updated_at""",
                (
                    artifact["id"],
                    artifact["organization_id"],
                    artifact.get("run_id"),
                    artifact["artifact_type"],
                    artifact["title"],
                    artifact["status"],
                    artifact["scope_id"],
                    payload,
                    artifact["created_at"],
                    artifact["updated_at"],
                ),
            )
