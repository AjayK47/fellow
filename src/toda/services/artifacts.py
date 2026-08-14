"""Durable, permission-scoped outputs produced by Tasks."""

from __future__ import annotations

import base64
import json
import mimetypes
from typing import Any

from toda.config import settings
from toda.domain.models import new_id, utcnow

# Extensions Slack and browsers get wrong when left to guess.
_EXTENSION_BY_MIME = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/html": ".html",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "application/json": ".json",
    "text/plain": ".txt",
}


def artifact_payload(artifact: dict[str, Any]) -> tuple[bytes, str, str]:
    """Render an artifact as (bytes, mime_type, filename).

    An artifact's content is one of four shapes, and getting this wrong is
    not subtle: a PDF is stored as {"base64": ..., "mime_type": ...}, so
    treating it as "not a string, therefore JSON" uploads the wrapper
    itself. That is exactly what reached a Slack channel -- a .json file
    whose entire body was {"base64": "JVBERi0x..."} where a readable PDF
    was expected.

    One implementation, used by both the HTTP download route and the Slack
    upload tool, because two copies is how they diverged.
    """
    content = artifact.get("content")
    if isinstance(content, dict) and isinstance(content.get("base64"), str):
        body = base64.b64decode(content["base64"])
        mime_type = str(content.get("mime_type") or "application/octet-stream")
    elif isinstance(content, dict) and isinstance(content.get("text"), str):
        body = content["text"].encode("utf-8")
        mime_type = str(content.get("mime_type") or "text/plain")
    elif isinstance(content, str):
        body = content.encode("utf-8")
        mime_type = "text/plain"
    else:
        body = json.dumps(content, indent=2, default=str).encode("utf-8")
        mime_type = "application/json"
    identifier = str(artifact.get("id") or "artifact")
    safe_title = "".join(
        c if c.isalnum() or c in " ._-" else "_"
        for c in str(artifact.get("title") or identifier)
    ).strip() or identifier
    extension = (
        _EXTENSION_BY_MIME.get(mime_type)
        or mimetypes.guess_extension(mime_type)
        or ""
    )
    if safe_title.lower().endswith(extension.lower()) and extension:
        return body, mime_type, safe_title
    return body, mime_type, f"{safe_title}{extension}"


class ArtifactService:
    def __init__(self, store: Any, audit: Any) -> None:
        self.store = store
        self.audit = audit

    @staticmethod
    def _with_download_url(artifact: dict[str, Any]) -> dict[str, Any]:
        """Computed fresh on every read/write rather than persisted -- the
        public URL (an ngrok tunnel, most likely) can rotate between
        restarts, and a stale absolute URL baked into old artifacts would
        silently 404 instead of reflecting whatever tunnel is live now."""
        if settings.server.public_url:
            artifact = dict(artifact)
            artifact["download_url"] = (
                f"{settings.server.public_url.rstrip('/')}/v1/artifacts/{artifact['id']}/download"
            )
        return artifact

    def create(
        self,
        *,
        organization_id: str,
        artifact_type: str,
        title: str,
        content: Any,
        scope_id: str,
        task_id: str | None = None,
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
            "task_id": task_id,
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
                "task_id": task_id,
            },
            principal_id=created_by,
        )
        return self._with_download_url(artifact)

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
        return self._with_download_url(artifact)

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        with self.store.cursor() as cur:
            cur.execute("SELECT payload FROM artifacts WHERE id = ?", (artifact_id,))
            row = cur.fetchone()
        if not row:
            return None
        return self._with_download_url(json.loads(row["payload"]))

    def list(
        self,
        organization_id: str,
        *,
        scope_ids: list[str] | None = None,
        artifact_type: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?", "status = 'active'"]
        params: list[Any] = [organization_id]
        if artifact_type:
            clauses.append("artifact_type = ?")
            params.append(artifact_type)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
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
            return [
                self._with_download_url(json.loads(row["payload"]))
                for row in cur.fetchall()
            ]

    def _write(self, artifact: dict[str, Any]) -> None:
        payload = json.dumps(artifact, default=str)
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO artifacts
                (id, organization_id, task_id, artifact_type, title, status,
                 scope_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                    status=excluded.status, payload=excluded.payload,
                    updated_at=excluded.updated_at""",
                (
                    artifact["id"],
                    artifact["organization_id"],
                    artifact.get("task_id"),
                    artifact["artifact_type"],
                    artifact["title"],
                    artifact["status"],
                    artifact["scope_id"],
                    payload,
                    artifact["created_at"],
                    artifact["updated_at"],
                ),
            )
