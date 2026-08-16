"""What a Task loads as its own history, and where that history may be cut.

Both rules here exist for the same reason: a tool call and its result are
one unit, and a transcript that splits them is rejected by every provider as
`invalid_request` -- which is_permanent_task_error treats as final, so the
job dead-letters and the conversation stops with nothing obviously wrong in
any log.

message_sanitization.repair_tool_call_pairing is the backstop that catches
whatever still reaches the wire broken. These two are what stop it being
produced in the first place.
"""

from __future__ import annotations

from toda.harness.runtime import _tool_safe_head


def _message(mid: str, *, task_id, role, at: str, call_id: str | None = None):
    message = {
        "id": mid,
        "organization_id": "org_acme",
        "session_id": "sess_scope",
        "task_id": task_id,
        "role": role,
        "content": f"{role} {mid}",
        "created_at": at,
    }
    if role == "assistant" and call_id:
        message["tool_calls"] = [
            {"id": call_id, "type": "function",
             "function": {"name": "brain_search", "arguments": "{}"}}
        ]
    if role == "tool":
        message["tool_call_id"] = call_id
    return message


def _seed_delegated_session(store) -> None:
    """A root Task that delegated to two children, all in one Session.

    Children share their parent's session_id (child_tasks.py) and carry
    their own task_id, which is the whole reason the root's history needs
    scoping.
    """
    for task_id, parent in (("task_root", None), ("task_a", "task_root"),
                            ("task_b", "task_root")):
        store.create_task({
            "id": task_id,
            "organization_id": "org_acme",
            "session_id": "sess_scope",
            "parent_task_id": parent,
            "root_task_id": "task_root",
            "state": "running",
            "worker_type": "session_manager" if parent is None else "general",
            "created_at": "2026-08-16T10:00:00+00:00",
            "updated_at": "2026-08-16T10:00:00+00:00",
        })
    for message in [
        _message("m1", task_id=None, role="user", at="2026-08-16T10:00:01+00:00"),
        _message("m2", task_id="task_root", role="assistant",
                 at="2026-08-16T10:00:02+00:00"),
        # Each child's handoff prompt is stored as a user turn.
        _message("m3", task_id="task_a", role="user", at="2026-08-16T10:00:03+00:00"),
        _message("m4", task_id="task_a", role="assistant",
                 at="2026-08-16T10:00:04+00:00", call_id="call_a"),
        # Concurrent: B's request lands before A's result comes back.
        _message("m5", task_id="task_b", role="assistant",
                 at="2026-08-16T10:00:05+00:00", call_id="call_b"),
        _message("m6", task_id="task_a", role="tool",
                 at="2026-08-16T10:00:06+00:00", call_id="call_a"),
        _message("m7", task_id="task_b", role="tool",
                 at="2026-08-16T10:00:07+00:00", call_id="call_b"),
    ]:
        store.append_session_message(message)


def test_a_root_task_does_not_load_its_specialists_transcripts(brain):
    """Delegation exists to keep a specialist's intermediate work out of the
    parent's context. The unfiltered query handed the root every search its
    children ran and every result they got back -- and, because children run
    concurrently, in an order that interleaved one child's request with
    another's, which no provider accepts."""
    _seed_delegated_session(brain.store)

    history = brain.store.list_session_messages(
        "sess_scope", limit=200, exclude_delegated=True
    )

    assert [item["id"] for item in history] == ["m1", "m2"]
    assert all(item["task_id"] in (None, "task_root") for item in history)


def test_a_child_still_loads_only_its_own_turns(brain):
    _seed_delegated_session(brain.store)

    history = brain.store.list_session_messages(
        "sess_scope", limit=200, task_id="task_a"
    )

    assert [item["id"] for item in history] == ["m3", "m4", "m6"]


def test_the_full_record_is_still_available_unscoped(brain):
    """The console, memory capture and session_search read the record rather
    than build a prompt, so they must keep seeing everything."""
    _seed_delegated_session(brain.store)

    history = brain.store.list_session_messages("sess_scope", limit=200)

    assert [item["id"] for item in history] == [
        "m1", "m2", "m3", "m4", "m5", "m6", "m7"
    ]


def test_a_trim_never_starts_on_an_orphaned_result():
    """Cutting by raw message count lands between a call and its result
    about as often as not in a tool-heavy transcript."""
    history = [
        {"role": "user"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1"},
        {"role": "tool", "tool_call_id": "c2"},
        {"role": "assistant"},
    ]
    # Keeping 2 would start on a bare tool result.
    head = _tool_safe_head(history, keep=2)
    assert history[head]["role"] == "assistant"
    assert history[head].get("tool_calls")


def test_a_trim_that_already_lands_cleanly_is_left_alone():
    history = [
        {"role": "user"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1"},
        {"role": "user"},
    ]
    assert _tool_safe_head(history, keep=1) == 3
    assert _tool_safe_head(history, keep=4) == 0
    assert _tool_safe_head(history, keep=99) == 0


def test_a_trim_resolves_backwards_so_it_can_never_empty_the_history():
    """Skipping *forward* past the orphans would be the other way to fix
    this, and on a transcript whose tail is all results it would leave no
    messages at all -- a request with nothing in it but system blocks."""
    history = [
        {"role": "assistant", "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c1"},
        {"role": "tool", "tool_call_id": "c2"},
    ]
    assert _tool_safe_head(history, keep=2) == 0
    assert _tool_safe_head(history, keep=1) == 0
