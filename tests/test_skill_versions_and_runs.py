"""Skill governance keeps history immutable and execution evaluation is explicit."""

from __future__ import annotations


def test_skill_proposal_approval_and_deprecation_keep_versions(brain):
    skill = brain.skills.create(organization_id="org_acme", name="Release", description="Release safely", goal="Ship", steps=[])
    proposal = brain.skills.propose_update(skill["id"], {"description": "Release safely with a rollback."})
    assert proposal and proposal["version"] == 2 and proposal["status"] == "draft"
    assert brain.skills.get(skill["id"])["version"] == 1
    approved = brain.skills.approve(skill["id"], 2)
    assert approved and approved["status"] == "active"
    assert brain.skills.get(skill["id"])["version"] == 2
    assert len(brain.skills.versions(skill["id"])) == 2
    deprecated = brain.skills.deprecate(skill["id"])
    assert deprecated and deprecated["status"] == "disabled"


def test_search_and_prepare_hide_unapproved_skills(brain):
    draft = brain.skills.create(organization_id="org_acme", name="Untested Rollback", description="d", goal="g", status="draft")
    assert draft["name"] not in [s["name"] for s in brain.skills.search("org_acme", "rollback")]
    assert brain.skills.prepare(draft["id"]) is None

    approved = brain.skills.create(organization_id="org_acme", name="Tested Rollback", description="d", goal="g")
    assert approved["name"] in [s["name"] for s in brain.skills.search("org_acme", "rollback")]
    assert brain.skills.prepare(approved["id"]) is not None

    brain.skills.approve(draft["id"], 1)
    assert draft["name"] in [s["name"] for s in brain.skills.search("org_acme", "rollback")]


def test_proposed_update_stays_hidden_until_approved(brain):
    skill = brain.skills.create(organization_id="org_acme", name="Deploy Freeze", description="Original", goal="g", steps=[])
    brain.skills.propose_update(skill["id"], {"description": "Updated with a caveat about weekends."})
    hits = brain.skills.search("org_acme", "deploy freeze")
    assert len(hits) == 1
    assert hits[0]["description"] == "Original"
