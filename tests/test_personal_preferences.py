"""M7: personal preferences."""

import json

from toda.harness.extraction import ModelMemoryExtractor
from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import ModelResponse


def _personal_scope(brain, principal_id):
    brain.store.upsert_scope(
        {
            "id": f"scope_person_{principal_id}",
            "organization_id": "org_acme",
            "kind": "person_private",
            "label": principal_id,
            "is_public": False,
        }
    )


def _preference_extractor(brain, author, text="I prefer bullet summaries"):
    model = ScriptedModelClient(
        [
            ModelResponse(
                content=json.dumps(
                    {
                        "proposed_entities": [],
                        "memories": [
                            {
                                "memory_type": "preference",
                                "content": text,
                                "confidence": 0.9,
                                "authority": 0.9,
                                "entity_ids": [],
                                "participant_ids": [],
                                "claim": {
                                    "subject": author,
                                    "predicate": "prefers",
                                    "object": {"value": "bullet summaries"},
                                    "decision": "ADD",
                                },
                                "valid_until": None,
                            }
                        ],
                    }
                ),
                model="test-model",
            )
        ]
    )
    return ModelMemoryExtractor(
        model=model, memory_service=brain.memory, store=brain.store,
        audit=brain.audit, reality=brain.reality,
    )


def test_dm_preference_lands_in_personal_scope_and_prompt(brain):
    _personal_scope(brain, "person_ajay")
    source = brain.ingestion.ingest(
        organization_id="org_acme", source_system="test", source_type="test",
        external_id="m7-pref-1", content="I prefer bullet summaries",
        scope_id="scope_person_person_ajay", author_entity_id="person_ajay",
        learn=False, processing_mode="reference",
    )["source"]
    memories = _preference_extractor(brain, "person_ajay").extract_source(source)
    assert len(memories) == 1
    fact = brain.reality.apply_claim("org_acme", memories[0]["claim"], memories[0], source=source)
    assert fact is not None
    assert fact["scope_id"] == "scope_person_person_ajay"

    prefs = brain.context.load_preferences("org_acme", "person_ajay")
    assert any(f["id"] == fact["id"] for f in prefs)
    assert brain.context.load_preferences("org_acme", "person_ravi") == []

    about = brain.context.render_about_you(prefs)
    assert "<about_you>" in about and "bullet summaries" in about


def test_principal_override_skips_approval_for_that_person_only(brain):
    from toda.harness.tools import runs_without_approval

    brain.tool_registry.register(
        __import__("toda.harness.models", fromlist=["ToolDefinition"]).ToolDefinition(
            name="pref_test_write",
            description="Preference test write",
            input_schema={"type": "object", "properties": {}},
            risk="write",
        ),
        lambda _args, _runtime: {"ok": True},
    )
    brain.store.upsert_simple_harness_record(
        "tool_policies",
        {
            "id": "tool_policy:org_acme:pref_test_write",
            "organization_id": "org_acme",
            "tool_name": "pref_test_write",
            "execution": "ask",
            "principal_overrides": {"person_ajay": "direct"},
            "updated_at": "2026-09-18T00:00:00+00:00",
        },
    )
    definition = next(
        d for d in brain.tool_executor.registry.definitions() if d.name == "pref_test_write"
    )
    assert not runs_without_approval(definition)
    ajay = brain.tool_executor.effective_policy("org_acme", definition, principal_id="person_ajay")
    ravi = brain.tool_executor.effective_policy("org_acme", definition, principal_id="person_ravi")
    assert ajay["execution"] == "direct" and ajay["source"] == "principal_override"
    assert ravi["execution"] == "ask"

    destructive = next(
        d for d in brain.tool_executor.registry.definitions() if d.name == "delete_file"
    )
    brain.store.upsert_simple_harness_record(
        "tool_policies",
        {
            "id": "tool_policy:org_acme:delete_file",
            "organization_id": "org_acme",
            "tool_name": "delete_file",
            "execution": "inherit",
            "principal_overrides": {"person_ajay": "direct"},
            "updated_at": "2026-09-18T00:00:00+00:00",
        },
    )
    assert brain.tool_executor.effective_policy(
        "org_acme", destructive, principal_id="person_ajay"
    )["execution"] != "direct"


def test_five_unedited_approvals_asks_once_then_yes_skips(brain):
    _personal_scope(brain, "person_ajay")
    for i in range(5):
        brain.reality.record_judgment(
            organization_id="org_acme",
            judgment_type="tool_approval",
            principal_id="person_ajay",
            subject={"tool_name": "post_draft", "arguments": {"text": f"draft {i}"}, "approval_id": f"appr_{i}"},
            decision="approved",
            reason="ok",
        )
    # channel resolution needs approval->task links; seed minimal ones
    from toda.domain.models import utcnow as _utcnow3

    now_iso = _utcnow3().isoformat()
    for i in range(5):
        task = brain.company_agent.create_invocation(
            envelope=__import__("tests.test_company_agent_harness", fromlist=["_envelope"])._envelope(
                brain, conversation=f"m7:askless:{i}"
            ),
            goal="post", trigger_kind="reactive", initial_message="post",
            metadata={"slack": {"channel_id": "C9"}},
        )
        approval = {
            "id": f"appr_{i}", "organization_id": "org_acme", "task_id": task["id"],
            "tool_call_id": f"call_{i}", "tool_name": "post_draft",
            "arguments": {"text": f"draft {i}"}, "status": "approved",
            "requested_by": "person_ajay", "allowed_approver_ids": ["person_ajay"],
            "preview": {"action": "post_draft", "description": "d", "arguments": {}},
            "created_at": now_iso, "updated_at": now_iso,
        }
        brain.store.upsert_approval(approval)
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {"id": "identity_slack_u7", "organization_id": "org_acme", "source": "slack",
         "source_user_id": "U7", "principal_id": "person_ajay",
         "workspace_id": "T", "team_id": "T"},
    )
    sent = []
    brain.preference_learner.slack_sender = type(
        "FakeSlack", (),
        {
            "open_dm": lambda self, ws, uid: {"ok": True, "channel_id": "D7"},
            "send": lambda self, **kw: (sent.append(kw), {"ok": True})[1],
        },
    )()
    out = brain.preference_learner.learn("org_acme", "person_ajay")
    assert out["suggestion"]["asked"] is True
    assert len(sent) == 1
    # second run: already suggested -> no second question
    out2 = brain.preference_learner.learn("org_acme", "person_ajay")
    assert out2["suggestion"]["asked"] is False
    assert len(sent) == 1

    # Yes -> override + fact; next approval skipped for that person
    yes = brain.slack_gateway._resolve_preference_interaction(
        {"team": {"id": "T"}, "user": {"id": "U7"}},
        {"action_id": "toda_preference_yes", "value": "person_ajay::post_draft::C9"},
    )
    assert yes["accepted"] is True
    definition = next(
        d for d in brain.tool_executor.registry.definitions() if d.name == "post_draft"
    )
    assert brain.tool_executor.effective_policy(
        "org_acme", definition, principal_id="person_ajay"
    )["execution"] == "direct"


def test_no_means_never_ask_again(brain):
    _personal_scope(brain, "person_ravi")
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {"id": "identity_slack_u8", "organization_id": "org_acme", "source": "slack",
         "source_user_id": "U8", "principal_id": "person_ravi",
         "workspace_id": "T", "team_id": "T"},
    )
    out = brain.slack_gateway._resolve_preference_interaction(
        {"team": {"id": "T"}, "user": {"id": "U8"}},
        {"action_id": "toda_preference_no", "value": "person_ravi::post_draft::C9"},
    )
    assert out["accepted"] is True
    prefs = brain.context.load_preferences("org_acme", "person_ravi")
    assert any("declined_auto_approve" in f["predicate"] for f in prefs)


def test_delete_inferred_preference_tombstones_against_reinference(brain):
    _personal_scope(brain, "person_ajay")
    fact = brain.reality.assert_fact(
        organization_id="org_acme", subject_id="person_ajay",
        predicate="prefers_shorter_drafts", object_value="true",
        scope_id="scope_person_person_ajay",
        valid_from="2026-09-01T00:00:00+00:00", source_ids=[],
        confidence=0.7, authority=0.4,
    )
    from toda.domain.models import utcnow as _utcnow

    now = _utcnow().isoformat()
    fact["status"] = "deleted"
    fact["system_to"] = now
    brain.reality._save_fact(fact)
    assert brain.preference_learner._has_tombstone("org_acme", "person_ajay", "prefers_shorter_drafts") is True
    from toda.domain.models import utcnow as _utcnow2

    out = brain.preference_learner._learn_habit(
        "org_acme", "person_ajay", [], _utcnow2()
    )
    assert out == {"learned": False, "reason": "tombstoned"}


def test_me_preferences_api_returns_only_caller_facts(api_client):
    from tests.conftest import sign_in

    store = api_client.app.state.brain.store
    store.upsert_scope(
        {
            "id": "scope_person_person_ajay",
            "organization_id": "org_acme",
            "kind": "person_private",
            "label": "ajay",
            "is_public": False,
        }
    )
    brain = api_client.app.state.brain
    fact = brain.reality.assert_fact(
        organization_id="org_acme", subject_id="person_ajay",
        predicate="prefers", object_value="bullet summaries",
        scope_id="scope_person_person_ajay",
        valid_from="2026-09-01T00:00:00+00:00", source_ids=[],
        confidence=0.9, authority=0.9,
    )
    response = api_client.get("/v1/me/preferences", params={"organization_id": "org_acme"})
    assert response.status_code == 200, response.text
    ids = [f["id"] for f in response.json()["facts"]]
    assert fact["id"] in ids

    sign_in(api_client, principal_id="person_ravi", role="member")
    response = api_client.get("/v1/me/preferences", params={"organization_id": "org_acme"})
    assert response.status_code == 200, response.text
    assert response.json()["facts"] == []

    sign_in(api_client, principal_id="person_ajay", role="owner")
    response = api_client.delete(
        f"/v1/me/preferences/{fact['id']}", params={"organization_id": "org_acme"}
    )
    assert response.status_code == 200, response.text
    response = api_client.get("/v1/me/preferences", params={"organization_id": "org_acme"})
    assert response.json()["facts"] == []
