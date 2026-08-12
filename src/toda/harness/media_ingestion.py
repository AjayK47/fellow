"""Background ingestion of Slack attachments into scoped artifacts."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse

import httpx

from toda.harness.media_providers import TranscriptionProvider
from toda.harness.models import NormalizedEvent


class SlackMediaIngestion:
    def __init__(self, *, store: Any, artifacts: Any, bot_token: str | None, transcription_provider: TranscriptionProvider | None, max_bytes: int) -> None:
        self.store = store
        self.artifacts = artifacts
        self.bot_token = bot_token
        self.transcription_provider = transcription_provider
        self.max_bytes = max_bytes

    def enrich(self, event: NormalizedEvent) -> NormalizedEvent:
        files = list(event.payload.get("files") or [])[:5]
        if not files:
            return event
        installation = self.store.slack_installation_by_workspace(event.scope.workspace_id or "")
        token = (installation or {}).get("bot_token") or self.bot_token
        notes: list[str] = []
        media_ids: list[str] = []
        transcript_ids: list[str] = []
        for item in files:
            title = str(item.get("name") or item.get("id") or "Slack attachment")
            mime = str(item.get("mimetype") or "application/octet-stream")
            try:
                payload = self._download(str(item.get("url_private_download") or item.get("url_private") or ""), token)
                artifact = self.artifacts.create(
                    organization_id=event.organization_id,
                    artifact_type="audio.slack" if mime.startswith("audio/") else "media.slack",
                    title=title,
                    content={"base64": base64.b64encode(payload).decode("ascii"), "mime_type": mime},
                    scope_id=event.scope.scope_id,
                    created_by=event.actor.id,
                    metadata={"source": "slack", "slack_file_id": item.get("id")},
                )
                media_ids.append(artifact["id"])
                if mime.startswith("audio/"):
                    if self.transcription_provider is None:
                        notes.append(f"Attached voice message {title!r}; transcription provider is not configured.")
                        continue
                    transcript = self.transcription_provider.transcribe(
                        payload=payload, filename=title, mime_type=mime, language=None
                    )
                    transcript_artifact = self.artifacts.create(
                        organization_id=event.organization_id,
                        artifact_type="document.transcript",
                        title=f"Transcript of {title}",
                        content={"text": transcript, "mime_type": "text/plain"},
                        scope_id=event.scope.scope_id,
                        created_by=event.actor.id,
                        metadata={
                            "source_artifact_id": artifact["id"],
                            "provider": self.transcription_provider.name,
                            "model": self.transcription_provider.model,
                        },
                    )
                    transcript_ids.append(transcript_artifact["id"])
                    notes.append(
                        f"Voice message transcript ({title}; source artifact {artifact['id']}; "
                        f"transcript artifact {transcript_artifact['id']}):\n{transcript}"
                    )
                else:
                    notes.append(f"Attached file {title!r} is available as artifact {artifact['id']}.")
            except Exception as exc:
                notes.append(f"Could not ingest Slack attachment {title!r}: {exc}")
        if notes:
            current = str(event.payload.get("text") or "").strip()
            event.payload["text"] = "\n\n".join(part for part in [current, *notes] if part)
        event.payload["media_artifact_ids"] = media_ids
        event.payload["transcript_artifact_ids"] = transcript_ids
        event.payload["files"] = [
            {key: item.get(key) for key in ("id", "name", "mimetype", "size", "duration_ms") if item.get(key) is not None}
            for item in files
        ]
        return event

    def _download(self, url: str, token: str | None) -> bytes:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "slack.com" or host.endswith(".slack.com")):
            raise ValueError("attachment URL is not a trusted Slack HTTPS URL")
        if not token:
            raise RuntimeError("Slack files:read connection is not configured")
        with httpx.stream("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=60.0, follow_redirects=False) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > self.max_bytes:
                raise ValueError("attachment exceeds configured media size limit")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > self.max_bytes:
                    raise ValueError("attachment exceeds configured media size limit")
                chunks.append(chunk)
        return b"".join(chunks)
