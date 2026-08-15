from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse, ModelToolCall


def _wake(brain, content: str, key: str = "slack:dm:person_ajay:conversation-first"):
    return brain.sessions.wake(
        organization_id="org_acme",
        conversation_key=key,
        destination={"type": "dm", "id": "D1", "is_private": True},
        role="user",
        content=content,
        principal_id="person_ajay",
    )


def test_greeting_is_conversation_and_creates_no_task(brain):
    brain.conversation_agent.model = ScriptedModelClient(
        [ModelResponse(content="Hello! How can I help?")]
    )
    wake = _wake(brain, "hi")

    outcome = brain.session_manager.dispatch(wake["session_id"])

    assert outcome.kind == "conversation"
    assert outcome.task_id is None
    assert outcome.answer == "Hello! How can I help?"
    assert brain.store.list_tasks("org_acme", session_id=wake["session_id"]) == []


def test_model_promotes_durable_work_with_start_task(brain):
    brain.conversation_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        name="start_task",
                        arguments={"goal": "Research the migration and create a PDF"},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="The durable work is complete.")]
    )
    wake = _wake(brain, "Research the migration and create a PDF", key="conv:promote")

    outcome = brain.session_manager.dispatch(wake["session_id"])

    assert outcome.kind == "task"
    assert outcome.task_id
    task = brain.store.get_task(outcome.task_id)
    assert task["goal"] == "Research the migration and create a PDF"
    assert task["origin_session_id"] == wake["session_id"]
    assert task["owner_id"] == "person_ajay"
    assert task["completion_summary"].startswith("Goal:")


def test_task_search_indexes_only_root_task_summaries(brain):
    brain.conversation_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[ModelToolCall(name="start_task", arguments={"goal": "Migrate Activity API"})],
                finish_reason="tool_calls",
            )
        ]
    )
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Activity API migration completed successfully.")]
    )
    wake = _wake(brain, "Please migrate Activity API", key="conv:task-search")
    outcome = brain.session_manager.dispatch(wake["session_id"])

    results = brain.store.search_root_tasks("org_acme", query="Activity migration")

    assert [item["id"] for item in results] == [outcome.task_id]


def test_a_conversation_turn_can_see_what_is_connected(brain):
    """Toda told a user "there is currently no CRM connected" while HubSpot
    was connected and active -- the conversation lane had no Composio tool
    at all, so it could not find out and guessed."""
    from toda.harness.conversation import _CONVERSATION_READ_TOOLS

    assert "composio_list_integrations" in _CONVERSATION_READ_TOOLS


def test_doing_connector_work_still_requires_a_task(brain):
    """The line is "settles a question" vs "does the work". Knowing what
    exists is discovery; searching for a specific action, reading its
    schema and executing it is the work, and work belongs to a Task with
    durability, delegation and progress reporting."""
    from toda.harness.conversation import _CONVERSATION_READ_TOOLS

    for name in (
        "COMPOSIO_SEARCH_TOOLS",
        "COMPOSIO_GET_TOOL_SCHEMAS",
        "COMPOSIO_EXECUTE",
        "delegate_tasks",
        "plan_create",
        "workspace_read",
        "pdf_extract",
    ):
        assert name not in _CONVERSATION_READ_TOOLS, name


def test_the_conversation_lane_cannot_grind(brain):
    """It has no Task behind it -- no durability, no delegation, no progress
    in the channel. A turn that cannot land in a few calls is a Task wearing
    the wrong clothes, and a generous iteration budget is what let one run
    twelve rounds of inline work instead of promoting."""
    assert brain.conversation_agent.max_iterations <= 5


def test_an_unfinished_turn_becomes_a_task_instead_of_raising(brain):
    """Raising on exhaustion failed the dispatch job, which retried, which
    re-ran the turn from scratch -- so a request the conversation lane could
    not finish became a loop rather than a Task.

    Observed live: the model wanted HubSpot's action list, could not reach
    composio_search from this lane, improvised with Brave and Firecrawl
    against Composio's own docs, ran out of budget, and started over."""
    from toda.harness.models import (
        DestinationEnvelope,
        InvocationEnvelope,
        ModelResponse,
        ModelToolCall,
        TriggerKind,
    )
    from toda.harness.task_run_store import TaskRunStore
    from toda.domain.models import new_id

    class NeverAnswers:
        """Keeps calling a tool and never produces a final answer."""

        model = "never-answers"

        def context_length(self):
            return 128_000

        def generate(self, **_kwargs):
            return ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id=new_id("call"), name="brain_search", arguments={"query": "x"}
                    )
                ],
            )

    session = brain.sessions.resolve(
        organization_id="org_acme",
        conversation_key="conv:exhaustion",
        destination={"type": "dm", "id": "d1", "is_private": True},
    )
    brain.sessions.append_message(
        organization_id="org_acme", session_id=session["id"],
        role="user", content="find every hubspot contact created this week",
    )
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", {"type": "dm", "id": "d1", "is_private": True}, "reactive_agent"
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme", principal_id="person_ajay",
        application_id="toda_api", conversation_id="conv:exhaustion",
        destination=DestinationEnvelope(type="dm", id="d1", is_private=True),
        purpose="reactive_agent", permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    runs = TaskRunStore(brain.store, owner=new_id("exec"), lease_seconds=600)

    outcome = brain.conversation_agent.run(
        session=session, envelope=envelope, runs=runs,
        trigger_kind=TriggerKind.REACTIVE, trigger_event_id=None,
        task_metadata={"worker_type": "conversation"},
        model=NeverAnswers(),
    )

    # A Task exists, rather than an exception having failed the job.
    assert outcome.task_id, "an unfinished turn must become a Task"
    task = brain.store.get_task(outcome.task_id)
    assert task is not None
    assert (task.get("metadata") or {}).get("promoted_reason") == "conversation_budget_exhausted"


def test_the_model_is_told_not_to_web_search_for_connector_capabilities(brain):
    """It searched docs.composio.dev with Brave to work out which HubSpot
    action to call, instead of starting a Task that can ask Composio."""
    from toda.harness.conversation import _START_TASK

    description = _START_TASK.description
    assert "connected app" in description
    assert "never search the web" in description.lower()
