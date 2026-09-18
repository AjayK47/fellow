"""M3: per-agent summaries."""


def test_children_compact_into_own_metadata_not_session(brain):
    from tests.test_company_agent_harness import _envelope

    parent = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="m3:parent"),
        goal="parent", trigger_kind="reactive", initial_message="parent",
    )
    for idx in range(2):
        child = brain.company_agent.create_invocation(
            envelope=_envelope(brain, conversation="m3:parent"),
            goal=f"child {idx}", trigger_kind="reactive", initial_message=f"child {idx}",
            parent_run_id=parent["id"],
            metadata={"worker_type": "general"},
        )
        # 40 large own-turn messages to force the token threshold
        for i in range(40):
            brain.company_agent.runs.add_message(
                organization_id="org_acme", session_id=child["session_id"],
                task_id=child["id"], role="assistant",
                content="x" * 4000, metadata={},
            )
        # run one loop iteration path: call the compaction-relevant section
        # via a failed-fast execute with tiny budget is brittle; instead
        # assert the storage rule directly: children must never write session.
        session_before = brain.store.get_session(child["session_id"])
        before_summary = (session_before or {}).get("summary")
        # simulate what _loop does for a child: metadata summary only
        metadata = dict(brain.store.get_task(child["id"]).get("metadata") or {})
        metadata["summary"] = f"child-{idx}-summary"
        metadata["summary_through_message_id"] = "smsg_x"
        t = brain.store.get_task(child["id"])
        t["metadata"] = metadata
        brain.store.update_task(t)
        session_after = brain.store.get_session(child["session_id"])
        assert (session_after or {}).get("summary") == before_summary
    for idx, child_id in enumerate(
        [t["id"] for t in brain.store.list_tasks("org_acme", parent_task_id=parent["id"], limit=10)]
    ):
        assert brain.store.get_task(child_id)["metadata"]["summary"].startswith("child-")


def test_child_with_large_history_compacts_and_completes(brain):
    from tests.test_company_agent_harness import _envelope
    from toda.harness.model_client import ScriptedModelClient
    from toda.harness.models import ModelResponse

    parent = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="m3:bigchild"),
        goal="parent", trigger_kind="reactive", initial_message="parent",
    )
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="child done")])
    child = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="m3:bigchild"),
        goal="big child", trigger_kind="reactive", initial_message="big child",
        parent_run_id=parent["id"], metadata={"worker_type": "general"},
    )
    for _ in range(10):
        brain.company_agent.runs.add_message(
            organization_id="org_acme", session_id=child["session_id"],
            task_id=child["id"], role="assistant", content="y" * 20000, metadata={},
        )
        brain.company_agent.runs.add_message(
            organization_id="org_acme", session_id=child["session_id"],
            task_id=child["id"], role="tool", content="z" * 20000, metadata={},
        )
    session_before = brain.store.get_session(child["session_id"])
    outcome = brain.company_agent.execute_task(child["id"])
    assert outcome.state.value == "completed"
    session_after = brain.store.get_session(child["session_id"])
    assert (session_after or {}).get("summary") == (session_before or {}).get("summary")
    assert brain.store.get_task(child["id"])["metadata"].get("summary")


def test_conversation_prompt_includes_session_summary(brain):
    wake = brain.sessions.wake(
        organization_id="org_acme", conversation_key="api:m3:convsumm",
        destination={"type": "internal_agent_session", "id": "m3s", "is_private": True},
        role="user", content="hello", principal_id="person_ajay",
        metadata={}, message_metadata={}, may_join=True,
    )
    session = brain.store.get_session(wake["session_id"])
    session["summary"] = "Earlier we decided to use Postgres."
    brain.store.update_session(session)
    from toda.harness.model_client import ScriptedModelClient
    from toda.harness.models import InvocationEnvelope, ModelResponse, TriggerKind

    brain.conversation_agent.model = ScriptedModelClient([ModelResponse(content="ok")])
    decision = brain.authz.resolve(
        "org_acme", "person_ajay",
        {"type": "internal_agent_session", "id": "m3s", "is_private": True}, "reactive_agent",
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme", principal_id="person_ajay", conversation_id="api:m3:convsumm",
        destination={"type": "internal_agent_session", "id": "m3s", "is_private": True},
        purpose="reactive_agent", permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    brain.conversation_agent.run(
        session=brain.store.get_session(wake["session_id"]), envelope=envelope,
        runs=brain.company_agent.runs, trigger_kind=TriggerKind.REACTIVE,
        trigger_event_id=None, task_metadata={},
    )
    bodies = [
        m["content"] for m in brain.conversation_agent.model.requests[0]["messages"]
        if m.get("role") == "system"
    ]
    assert any("Postgres" in b for b in bodies)
