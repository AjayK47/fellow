from toda.harness.context_budget import (
    context_budget_for_request,
    estimate_tokens,
    request_budget,
)
import pytest


def test_request_budget_reserves_output_before_input():
    budget = request_budget(
        context_window=64_000,
        output_tokens=16_000,
        messages=[{"role": "user", "content": "hello"}],
    )
    assert budget.output_reservation == 16_000
    assert budget.effective_input_window == 47_488
    assert budget.fits


def test_tool_schemas_count_as_request_input():
    small = request_budget(
        context_window=64_000,
        output_tokens=4_000,
        messages=[{"role": "user", "content": "hello"}],
    )
    large = request_budget(
        context_window=64_000,
        output_tokens=4_000,
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "x" * 40_000,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert large.estimated_input_tokens > small.estimated_input_tokens


def test_company_context_budget_uses_remaining_request_space():
    budget = context_budget_for_request(
        context_window=8_000,
        output_tokens=2_000,
        messages_without_company_context=[
            {"role": "system", "content": "fixed prompt" * 100}
        ],
        configured_context_budget=10_000,
    )
    assert 0 < budget < 10_000


def test_estimator_includes_json_keys_and_empty_values():
    assert estimate_tokens({"messages": [{"role": "user", "content": "x"}]}) > 1


@pytest.mark.parametrize("window", [64_000, 128_000, 200_000, 512_000, 1_000_000])
def test_standard_request_stays_inside_supported_provider_windows(window):
    budget = request_budget(
        context_window=window,
        output_tokens=min(40_000, window // 4),
        messages=[
            {"role": "system", "content": "You are the Session Manager."},
            {"role": "user", "content": "Investigate the current launch status."},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "brain_search",
                    "description": "Search permission-filtered company context.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert budget.fits
    assert budget.remaining_for_context > 0


def test_oversized_fixed_request_leaves_no_retrieval_budget():
    budget = context_budget_for_request(
        context_window=64_000,
        output_tokens=16_000,
        messages_without_company_context=[
            {"role": "system", "content": "x" * 250_000}
        ],
        configured_context_budget=200_000,
    )
    assert budget == 0


def test_root_tasks_are_not_treated_as_child_workers():
    """The context-shrink retry, the child-context skip and the Slack
    history load all branch on "is this a delegated specialist".

    That predicate was written out inline three times, and one copy drifted
    into `not metadata.get("worker_type")` -- "no worker type at all" rather
    than "not a child". Every root Task carries one
    (SessionManagerScheduler sets "session_manager", ConversationAgent sets
    "conversation"), so the retry silently never ran for a single Slack
    conversation and an over-budget request skipped straight to truncating
    history. Pin the predicate itself.
    """
    from toda.harness.runtime import _is_child_worker

    assert _is_child_worker({"worker_type": "general"})
    assert _is_child_worker({"worker_type": "pi"})
    assert _is_child_worker({"metadata": {"worker_type": "general"}})

    assert not _is_child_worker({"worker_type": "session_manager"})
    assert not _is_child_worker({"metadata": {"worker_type": "session_manager"}})
    assert not _is_child_worker({"metadata": {"worker_type": "conversation"}})
    assert not _is_child_worker({})


def test_volatile_state_sits_after_every_cacheable_block():
    """Prefix caching matches an exact token prefix, so one mutating value
    early in the request invalidates everything after it.

    The iteration counter, tool-call counter and clock used to live in
    `<trusted_invocation>` at message #2 -- immediately after the system
    prompt. That made the cacheable prefix the system prompt and nothing
    else: measured at ~5k of a ~43k-token request. Skills, organization
    memory and the entire company_context block were re-billed uncached on
    every iteration of every Task.
    """
    import json

    from toda.harness.context_budget import estimate_tokens
    from toda.harness.prompts import build_agent_messages

    def run_at(iteration: int, calls: int) -> dict:
        return {
            "id": "task_x", "goal": "g", "trigger_kind": "reactive",
            "max_iterations": 50, "max_tool_calls": 100,
            "iteration_count": iteration, "tool_call_count": calls,
            "organization_id": "org", "session_id": "s", "metadata": {},
            "envelope": {
                "principal_id": "p1",
                "destination": {"type": "dm", "id": "d"},
                "purpose": "reactive_agent",
                "allowed_scope_ids": ["scope_org_public"],
            },
        }

    context = {"request_id": "r", "permission_decision_id": "d", "summary": "x" * 20_000}
    first = build_agent_messages(run=run_at(1, 0), context=context, history=[])
    second = build_agent_messages(run=run_at(2, 3), context=context, history=[])

    shared = 0
    for a, b in zip(first, second):
        if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
            break
        shared += estimate_tokens([a])

    total = estimate_tokens(first)
    assert shared / total > 0.9, (
        f"only {shared / total:.1%} of the request is a stable prefix; "
        "something volatile moved ahead of a cacheable block"
    )
    # The one block that legitimately differs must be the last.
    assert "<runtime_state>" in first[-1]["content"]
    assert first[-1] != second[-1]


def test_runtime_state_still_carries_the_clock_and_counters():
    """Moving them must not drop them -- the model still needs to know what
    time it is and how much budget it has spent."""
    import json

    from toda.harness.prompts import build_agent_messages

    messages = build_agent_messages(
        run={
            "id": "t", "goal": "g", "trigger_kind": "reactive",
            "max_iterations": 50, "max_tool_calls": 100,
            "iteration_count": 7, "tool_call_count": 11,
            "organization_id": "org", "session_id": "s", "metadata": {},
            "envelope": {
                "principal_id": "p1",
                "destination": {"type": "dm", "id": "d"},
                "purpose": "reactive_agent",
                "allowed_scope_ids": [],
            },
        },
        context={},
        history=[],
    )
    state = json.loads(
        messages[-1]["content"].split("<runtime_state>\n")[1].split("\n</runtime_state>")[0]
    )
    assert state["used_iterations"] == 7
    assert state["used_tool_calls"] == 11
    assert state["current_time"]["utc"]
    # The limits stay in the stable block, since they never change.
    invocation = next(
        m["content"] for m in messages if m["content"].startswith("<trusted_invocation>")
    )
    assert '"max_iterations": 50' in invocation
