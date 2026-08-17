"""File-backed immutable skill package and readiness contracts."""

from __future__ import annotations

import pytest

from toda.services.skill_packages import SkillPackageCatalog


def test_catalog_loads_adopted_skill_surface_with_hashes():
    catalog = SkillPackageCatalog()
    names = {package.manifest.name for package in catalog.list()}
    assert len(names) >= 40
    assert "slack-communication" in names
    assert "systematic-debugging" in names
    assert all(len(package.content_hash) == 64 for package in catalog.list())
    assert all(package.manifest.kind in {"builtin", "connector"} for package in catalog.list())


def test_catalog_rejects_resource_escape():
    catalog = SkillPackageCatalog()
    with pytest.raises(ValueError, match="escapes"):
        catalog.resource("slack-communication", "../../pyproject.toml")


def test_readiness_reports_missing_capabilities():
    catalog = SkillPackageCatalog()
    readiness = {item["name"]: item for item in catalog.readiness(set())}
    assert readiness["deep-research"]["ready"] is False
    assert readiness["slack-communication"]["ready"] is True


def test_empty_database_can_materialize_packages_without_company_data(empty_brain):
    active = empty_brain.skills.resolve_for_task(
        organization_id="org_acme",
        task="write a Slack response",
        destination={"type": "dm", "id": "D1", "is_private": True},
        trigger_kind="reactive",
        purpose="answer_question",
        allowed_scope_ids=[],
    )
    assert any(skill["name"] == "Write for Slack" for skill in active)
    assert all(skill.get("immutable") for skill in active)
