"""What pauses for a human, and what does not.

The allow-list drifted: it began as four automation tools and grew by hand
each time an obvious case surfaced, while everything unlisted kept pausing.
The rule is now structural -- an action that stays inside Toda runs
directly; one that leaves Toda stops -- so these tests pin the boundary
rather than an enumeration.
"""

from __future__ import annotations

import pytest

from toda.harness.tools import runs_without_approval


def _execution(brain, name: str) -> str | None:
    registered = brain.tool_registry.get(name)
    if registered is None:
        return None
    return brain.tool_executor.effective_policy("org_acme", registered.definition)[
        "execution"
    ]


@pytest.mark.parametrize(
    "name",
    [
        # Authoring into Toda's own artifact store, and editing what it made.
        "pdf_create", "docx_create", "xlsx_create", "pptx_create", "html_create",
        "pdf_edit", "docx_edit", "xlsx_edit", "pptx_edit",
        "pdf_merge", "pdf_split", "pdf_fill_form",
        "chart_create", "diagram_render", "infographic_create", "image_generate",
        "brain_create_artifact",
        # Toda's own workspace, memory, tasks, schedule.
        "workspace_write", "workspace_patch",
        "brain_update_case",
        "task_cancel", "task_continue",
        "automation_create", "automation_delete",
        # Replying in the channel the request came from.
        "slack_send_message", "slack_send_artifact", "slack_react",
    ],
)
def test_work_that_stays_inside_toda_runs_directly(brain, name):
    execution = _execution(brain, name)
    if execution is None:
        pytest.skip(f"{name} is not registered in this environment")
    assert execution == "direct", f"{name} should not pause"


@pytest.mark.parametrize(
    "name",
    [
        # Someone else's CRM, inbox, calendar.
        "COMPOSIO_EXECUTE",
        # Grants Toda a connection to an external account.
        "request_app_connection",
        # Durable instruction applied to every later run -- a real decision.
        "skill_propose_create",
        "skill_propose_update",
    ],
)
def test_work_that_leaves_toda_still_stops(brain, name):
    execution = _execution(brain, name)
    if execution is None:
        pytest.skip(f"{name} is not registered in this environment")
    assert execution != "direct", f"{name} must not run unattended"


def test_the_boundary_is_the_capability_not_a_name(brain):
    """A connector tool nobody has added yet is gated on arrival, and a new
    native tool runs directly -- which is the point of a rule."""
    from toda.harness.models import ToolDefinition

    invented_connector = ToolDefinition(
        name="SOME_FUTURE_CRM_WRITE",
        description="x",
        input_schema={"type": "object", "properties": {}},
        risk="write",
        capability_id="connector.composio.action",
    )
    invented_native = ToolDefinition(
        name="some_future_document_op",
        description="x",
        input_schema={"type": "object", "properties": {}},
        risk="write",
        capability_id="document.pdf.annotate",
    )
    assert not runs_without_approval(invented_connector)
    assert runs_without_approval(invented_native)


def test_arbitrary_execution_is_never_waived(brain):
    """Approval is not a substitute for a sandbox, which is exactly why it
    cannot be waived for code execution."""
    from toda.harness.models import ToolDefinition

    assert not runs_without_approval(
        ToolDefinition(
            name="run_code",
            description="x",
            input_schema={"type": "object", "properties": {}},
            risk="write",
            capability_id="code.run",
        )
    )


def test_an_unclassified_tool_is_gated_not_waived():
    """The asymmetry that matters. Wrongly pausing a contained action costs
    a moment of friction; wrongly waiving an uncontained one is an action
    nobody agreed to. A tool carrying no capability at all must therefore
    stop, which an earlier "anything not external is native" rule did not
    do."""
    from toda.harness.models import ToolDefinition

    assert not runs_without_approval(
        ToolDefinition(
            name="policy_test_write",
            description="x",
            input_schema={"type": "object", "properties": {}},
            risk="write",
        )
    )
    assert not runs_without_approval(
        ToolDefinition(
            name="something_new",
            description="x",
            input_schema={"type": "object", "properties": {}},
            risk="write",
            capability_id="unrecognised.family",
        )
    )
