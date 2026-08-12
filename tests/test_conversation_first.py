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
