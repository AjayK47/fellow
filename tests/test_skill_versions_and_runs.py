"""Skill governance keeps history immutable and execution evaluation is explicit."""

from __future__ import annotations


def test_skill_proposal_approval_and_deprecation_keep_versions(brain):
    skill = brain.skills.create(organization_id="org_acme", name="Release", description="Release safely", goal="Ship", steps=[])
    proposal = brain.skills.propose_update(skill["id"], {"description": "Release safely with a rollback."})
    assert proposal and proposal["version"] == 2 and proposal["status"] == "draft"
    assert brain.skills.get(skill["id"])["version"] == 1
    approved = brain.skills.approve(skill["id"], 2)
    assert approved and approved["status"] == "approved"
    assert brain.skills.get(skill["id"])["version"] == 2
    assert len(brain.skills.versions(skill["id"])) == 2
    deprecated = brain.skills.deprecate(skill["id"])
    assert deprecated and deprecated["status"] == "deprecated"


def test_run_evaluation_requires_human_skill_governance(brain):
    skill = brain.skills.create(organization_id="org_acme", name="Recover", description="Recover", goal="Restore", steps=[])
    run = brain.runs.start(organization_id="org_acme", task="Recover export", acting_principal_id="person_ajay", harness_id="h", skill_id=skill["id"])
    brain.runs.add_event(run["id"], {"event_type": "action.completed", "action": "rollback", "result": "failed", "evidence_source_ids": ["src_x"]})
    brain.runs.complete(run["id"], outcome="failed")
    evaluation = brain.runs.evaluate(run["id"])
    assert evaluation and evaluation["skill_update_candidate"]["recommended"] is True
    assert evaluation["evidence_source_ids"] == ["src_x"]
