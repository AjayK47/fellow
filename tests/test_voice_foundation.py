"""U1 voice foundation tests."""

from toda.harness.prompts import build_agent_messages, render_voice
from toda.harness.retry import failure_category
from toda.harness.delivery import _FAILURE_TEXTS


def _run(worker_type="session_manager", parent=None):
    return {
        "id": "task_1",
        "goal": "g",
        "trigger_kind": "reactive",
        "worker_type": worker_type,
        "parent_task_id": parent,
        "metadata": {"worker_type": worker_type},
        "max_iterations": 5,
        "max_tool_calls": 5,
        "envelope": {
            "principal_id": "p",
            "destination": {"type": "dm", "id": "D1"},
            "purpose": "test",
            "allowed_scope_ids": [],
        },
    }


def test_voice_in_root_absent_in_child():
    root = build_agent_messages(run=_run("session_manager"), context={}, history=[])
    assert any("colleague on this team" in m.get("content", "") for m in root)
    child = build_agent_messages(
        run=_run("general", parent="task_0"), context={}, history=[]
    )
    assert not any("colleague on this team" in m.get("content", "") for m in child)


def test_persona_name_renders():
    assert "Mio" in render_voice("Mio")
    msgs = build_agent_messages(
        run=_run("session_manager"), context={}, history=[], persona_name="Mio"
    )
    assert any("Mio" in m.get("content", "") for m in msgs)


def test_failure_category_markers():
    assert failure_category("iteration_limit reached") == "budget"
    assert failure_category("tool_call_limit hit") == "budget"
    assert failure_category("context_window_exceeded blah") == "too_long"
    assert failure_category("connection reset by peer") == "provider"
    assert failure_category("not authorized to read") == "permission"
    assert failure_category("403 forbidden") == "permission"
    assert failure_category("invalid_request bad schema") == "invalid_request"
    assert failure_category("something totally new") == "unknown"


def test_failure_texts_have_no_code_fence_or_raw_error():
    for category, text in _FAILURE_TEXTS.items():
        assert "```" not in text, category
        assert "iteration_limit" not in text.lower(), category
