"""M2: capture only what happened."""


def _task(brain, *, goal="Do thing", final_answer="Priya owns the billing service"):
    task = brain.company_agent.create_invocation(
        envelope=__import__("tests.test_company_agent_harness", fromlist=["_envelope"])._envelope(
            brain, conversation="m2:test"
        ),
        goal=goal,
        trigger_kind="reactive",
        initial_message=goal,
    )
    task = brain.store.get_task(task["id"])
    task["state"] = "completed"
    task["final_answer"] = final_answer
    brain.store.update_task(task)
    return task


def test_final_answer_claim_creates_no_fact(brain):
    task = _task(brain)
    result = brain.harness_memory.capture_task(task["id"])
    assert result["captured"] is True
    facts = brain.reality.current_facts("org_acme")
    assert not [f for f in facts if "billing" in str(f.get("object_value") or "").lower()]
    derived = [brain.store.get_memory(mid) for mid in result["memory_ids"]]
    derived = [m for m in derived if m and m.get("memory_type") == "derived"]
    assert derived and derived[0]["authority"] == 0.3


def test_write_tool_creates_exactly_one_outcome(brain):
    task = _task(brain, final_answer="Done")
    now = "2026-09-18T00:00:00+00:00"
    brain.store.upsert_harness_tool_call(
        {
            "id": "call_1",
            "organization_id": "org_acme",
            "task_id": task["id"],
            "tool_name": "slack_send_message",
            "arguments": {"channel_id": "#general", "text": "hi"},
            "risk": "write",
            "status": "completed",
            "idempotency_key": f"{task['id']}:call_1",
            "created_at": now,
            "updated_at": now,
        }
    )
    result = brain.harness_memory.capture_task(task["id"])
    outcomes = [
        brain.store.get_memory(mid)
        for mid in result["memory_ids"]
    ]
    outcomes = [m for m in outcomes if m and m.get("memory_type") == "outcome"]
    assert len(outcomes) == 1
    assert "general" in outcomes[0]["content"]


def test_user_message_extracted_with_user_as_author(brain):
    task = _task(brain, final_answer="Noted")
    brain.company_agent.runs.add_message(
        organization_id="org_acme",
        session_id=task["session_id"],
        task_id=task["id"],
        role="user",
        content="we decided to drop Postgres",
        metadata={"principal_id": "person_ajay"},
    )
    result = brain.harness_memory.capture_task(task["id"])
    assert result["captured"] is True
    mems = [brain.store.get_memory(mid) for mid in result["memory_ids"]]
    assert any(
        m and "Postgres" in (m.get("content") or "") for m in mems
    )
