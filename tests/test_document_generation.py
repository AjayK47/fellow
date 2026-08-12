"""Typed document generation (pdf_create/docx_create/xlsx_create/pptx_create)
actually produces valid files once its optional runtime dependencies are
installed -- not just that dependency_ready() reports true. These tests call
the handler through ToolExecutor with policy already resolved; organization
mode/reviewer behavior is covered separately in test_tool_policy_modes.py."""

from __future__ import annotations

import base64
import zipfile
import io

import pytest

from toda.harness.models import InvocationEnvelope, ModelToolCall


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
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "completed"
    data = _content_bytes(brain, result.output["artifact_id"])
    assert data.startswith(b"%PDF")


def test_pdf_create_with_html_renders_real_multi_page_layout(brain):
    """The designed-HTML path (WeasyPrint), not the plain-text fallback --
    proves @page sizing and break-after pagination actually take effect,
    not just that some PDF bytes come back."""
    run = _run(brain)
    html = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page { size: letter; margin: 2cm; }
  body { font-family: sans-serif; }
  h1 { color: #1a1a1a; }
  .page-break { break-after: page; }
</style></head>
<body>
<h1>Page one</h1><p class="page-break">First page content.</p>
<h1>Page two</h1><p>Second page content.</p>
</body></html>"""
    call = ModelToolCall(
        id="call_pdf_html",
        name="pdf_create",
        arguments={"title": "Designed Report", "html": html},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "completed", result.error
    data = _content_bytes(brain, result.output["artifact_id"])
    assert data.startswith(b"%PDF")
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 2


def test_html_create_with_text_argument_persists_the_content(brain):
    run = _run(brain)
    call = ModelToolCall(
        id="call_html_text",
        name="html_create",
        arguments={"title": "A Page", "text": "<h1>Hello</h1>"},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "completed", result.error
    artifact = brain.artifacts.get(result.output["artifact_id"])
    assert artifact["content"]["text"] == "<h1>Hello</h1>"


def test_html_create_with_html_argument_also_persists_the_content(brain):
    """Regression test for a real production bug: pdf_create's schema adds
    an `html` property to the *shared* DocumentTools properties dict (every
    operation's input_schema is built from the same common dict), so a
    model calling html_create with `html=` instead of the documented
    `text=` argument silently produced an artifact with EMPTY content --
    confirmed from an actual delivered link that rendered a blank page.
    html_create must accept either key."""
    run = _run(brain)
    call = ModelToolCall(
        id="call_html_html_key",
        name="html_create",
        arguments={"title": "A Page", "html": "<h1>Hello</h1>"},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "completed", result.error
    artifact = brain.artifacts.get(result.output["artifact_id"])
    assert artifact["content"]["text"] == "<h1>Hello</h1>"


def test_docx_create_produces_a_real_docx_artifact(brain):
    run = _run(brain)
    call = ModelToolCall(
        id="call_docx",
        name="docx_create",
        arguments={"title": "Report", "text": "Executive summary\nDetails follow."},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
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
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
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
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "completed"
    data = _content_bytes(brain, result.output["artifact_id"])
    assert zipfile.is_zipfile(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "ppt/presentation.xml" in archive.namelist()
