"""Artifacts reach Slack as the file they are, and without a pointless pause."""

from __future__ import annotations

import base64
import json

from toda.harness.tools import NATIVE_ALLOW_TOOL_NAMES
from toda.services.artifacts import artifact_payload

_PDF_BYTES = b"%PDF-1.4\nfake pdf body\n%%EOF"


def _pdf_artifact() -> dict:
    return {
        "id": "artifact_1",
        "title": "US CHIPS Act & Taiwan Economic Impact Report",
        "content": {
            "base64": base64.b64encode(_PDF_BYTES).decode(),
            "mime_type": "application/pdf",
        },
    }


def test_a_binary_artifact_renders_as_its_real_bytes():
    """The wrapper is not the document. Treating "not a string, therefore
    JSON" uploaded {"base64": "JVBERi0x..."} into a Slack channel as a
    .json file where a readable PDF was expected."""
    body, mime, filename = artifact_payload(_pdf_artifact())
    assert body == _PDF_BYTES
    assert body.startswith(b"%PDF")
    assert mime == "application/pdf"
    assert filename.endswith(".pdf")
    assert "base64" not in filename


def test_the_other_three_content_shapes():
    text_body, text_mime, text_name = artifact_payload(
        {"id": "a", "title": "Notes", "content": "plain"}
    )
    assert text_body == b"plain" and text_mime == "text/plain" and text_name.endswith(".txt")

    wrapped, wrapped_mime, _ = artifact_payload(
        {"id": "b", "title": "Page", "content": {"text": "<p>hi</p>", "mime_type": "text/html"}}
    )
    assert wrapped == b"<p>hi</p>" and wrapped_mime == "text/html"

    structured, structured_mime, structured_name = artifact_payload(
        {"id": "c", "title": "Data", "content": {"rows": [1, 2]}}
    )
    assert json.loads(structured) == {"rows": [1, 2]}
    assert structured_mime == "application/json" and structured_name.endswith(".json")


def test_an_extension_is_not_doubled():
    _, _, filename = artifact_payload(
        {"id": "d", "title": "report.pdf",
         "content": {"base64": base64.b64encode(_PDF_BYTES).decode(),
                     "mime_type": "application/pdf"}}
    )
    assert filename == "report.pdf"


def test_slack_upload_sends_the_document_not_its_wrapper(brain, monkeypatch):
    artifact = brain.artifacts.create(
        organization_id="org_acme",
        title="US CHIPS Act Report",
        artifact_type="report",
        content={"base64": base64.b64encode(_PDF_BYTES).decode(),
                 "mime_type": "application/pdf"},
        scope_id="scope_org_public",
        created_by="person_ajay",
    )
    uploaded: list[dict] = []
    brain.slack_sender.upload_file = lambda **kw: (uploaded.append(kw), {"ok": True})[1]

    registered = brain.tool_registry.get("slack_send_artifact")
    assert registered is not None
    registered.handler(
        {"artifact_id": artifact["id"]},
        {
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "destination": {"type": "slack_channel", "id": "C1", "is_private": False},
            "purpose": "reactive_agent",
            "task_metadata": {"slack": {"channel_id": "C1", "workspace_id": "T1"}},
        },
    )

    assert uploaded[0]["content"] == _PDF_BYTES
    assert uploaded[0]["filename"].endswith(".pdf")


def test_authoring_and_replying_do_not_pause_for_approval(brain):
    """Producing the document that was asked for is the deliverable, not a
    consequential action. Approvals lose meaning when they fire on the
    obvious."""
    for name in ("pdf_create", "docx_create", "brain_create_artifact",
                 "slack_send_artifact", "slack_send_message"):
        registered = brain.tool_registry.get(name)
        assert registered is not None, name
        policy = brain.tool_executor.effective_policy("org_acme", registered.definition)
        assert policy["execution"] == "direct", f"{name} -> {policy['execution']}"


def test_reaching_an_external_system_is_still_gated(brain):
    """The allow-list is scoped to Toda's own state and the current channel."""
    registered = brain.tool_registry.get("COMPOSIO_EXECUTE")
    if registered is None:
        return
    policy = brain.tool_executor.effective_policy("org_acme", registered.definition)
    assert policy["execution"] != "direct"
    assert "COMPOSIO_EXECUTE" not in NATIVE_ALLOW_TOOL_NAMES
