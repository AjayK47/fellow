"""Typed document generation (pdf_create/docx_create/xlsx_create/pptx_create)
actually produces valid files once its optional runtime dependencies
(reportlab, python-docx, openpyxl, python-pptx) are installed -- not just
that dependency_ready() reports true. These tools now auto-execute like
every other ordinary write tool: TypedDocumentTools' own independent
approval gate was retired in favor of the single unified gate in
ToolExecutor.execute() (risk tier + ActionReviewer front door)."""

from __future__ import annotations

import base64
import zipfile
import io

import pytest

from docket.harness.models import InvocationEnvelope, ModelToolCall


def _envelope(brain) -> InvocationEnvelope:
    destination = {"type": "internal_agent_session", "id": "docgen-test", "is_private": True}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id="api:docgen:thread",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _run(brain):
    created = brain.company_agent.create_invocation(
        envelope=_envelope(brain),
        goal="generate a document",
        trigger_kind="reactive",
        initial_message="generate a document",
    )
    return brain.store.get_task(created["id"])


def _content_bytes(brain, artifact_id: str) -> bytes:
    artifact = brain.artifacts.get(artifact_id)
    return base64.b64decode(artifact["content"]["base64"])


@pytest.fixture(autouse=True)
def _require_document_runtime(brain):
    ready, missing = brain.document_tools.dependency_ready("pdf_create")
    if not ready:
        pytest.skip(f"document runtime not installed: {missing}")


def test_pdf_create_produces_a_real_pdf_artifact(brain):
    run = _run(brain)
    call = ModelToolCall(
        id="call_pdf",
        name="pdf_create",
        arguments={"title": "Computer-Use SaaS Comparison", "text": "Line one\nLine two"},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status == "completed"
    data = _content_bytes(brain, result.output["artifact_id"])
    assert data.startswith(b"%PDF")


def test_docx_create_produces_a_real_docx_artifact(brain):
    run = _run(brain)
    call = ModelToolCall(
        id="call_docx",
        name="docx_create",
        arguments={"title": "Report", "text": "Executive summary\nDetails follow."},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status == "completed"
    data = _content_bytes(brain, result.output["artifact_id"])
    assert zipfile.is_zipfile(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "word/document.xml" in archive.namelist()


def test_xlsx_create_produces_a_real_xlsx_artifact(brain):
    run = _run(brain)
    call = ModelToolCall(
        id="call_xlsx",
        name="xlsx_create",
        arguments={"title": "Pricing", "rows": [["Product", "Price"], ["Coasty AI", "$99"]]},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status == "completed"
    data = _content_bytes(brain, result.output["artifact_id"])
    assert zipfile.is_zipfile(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "xl/workbook.xml" in archive.namelist()


def test_pptx_create_produces_a_real_pptx_artifact(brain):
    run = _run(brain)
    call = ModelToolCall(
        id="call_pptx",
        name="pptx_create",
        arguments={"slides": [{"title": "Overview", "text": "Key findings"}]},
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status == "completed"
    data = _content_bytes(brain, result.output["artifact_id"])
    assert zipfile.is_zipfile(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "ppt/presentation.xml" in archive.namelist()
