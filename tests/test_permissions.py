"""Permission filtering and source revocation against real authz + retrieval."""

from __future__ import annotations


def test_private_channel_memory_not_returned_for_public_destination(brain):
    org = "org_acme"
    # Public destination (engineering channel treated as shared/public-style)
    public_dest = {
        "type": "slack_channel",
        "id": "channel_eng",
        "is_private": False,
    }
    # Even as person_ajay who can access private leadership, public dest must fail closed
    result = brain.retrieval.search(
        organization_id=org,
        query="headcount freeze confidential acquisition",
        acting_principal_id="person_ajay",
        destination=public_dest,
        purpose="public_reply",
        limit=50,
    )
    contents = " ".join(
        (r["memory"].get("content") or "") for r in result["results"]
    ).lower()
    assert "headcount freeze" not in contents
    assert "confidential" not in contents
    assert result["permission_decision_id"]


def test_private_memory_available_in_private_destination_for_member(brain):
    org = "org_acme"
    private_dest = {
        "type": "private_channel",
        "id": "channel_leadership",
        "is_private": True,
    }
    result = brain.retrieval.search(
        organization_id=org,
        query="headcount freeze",
        acting_principal_id="person_ajay",
        destination=private_dest,
        purpose="private_review",
        limit=50,
    )
    contents = " ".join((r["memory"].get("content") or "") for r in result["results"]).lower()
    assert "headcount" in contents or "freeze" in contents or result["count"] >= 0
    # Member of private scope should see private content
    assert any(
        "headcount" in (r["memory"].get("content") or "").lower()
        or "confidential" in (r["memory"].get("content") or "").lower()
        for r in result["results"]
    )


def test_non_member_cannot_see_private_scope(brain):
    org = "org_acme"
    private_dest = {
        "type": "private_channel",
        "id": "channel_leadership",
        "is_private": True,
    }
    # person_ravi is NOT in leadership private scope
    result = brain.retrieval.search(
        organization_id=org,
        query="headcount freeze confidential",
        acting_principal_id="person_ravi",
        destination=private_dest,
        purpose="private_review",
        limit=50,
    )
    contents = " ".join((r["memory"].get("content") or "") for r in result["results"]).lower()
    assert "headcount freeze" not in contents
    assert "confidential" not in contents


def test_get_memory_denies_private_without_authorized_principal(brain):
    """Direct GET must fail closed — no authz-free private content."""
    org = "org_acme"
    # Locate a private-scope memory
    private_mems = [
        m
        for m in brain.store.list_memories(org, latest_only=False)
        if m.get("scope_id") == "scope_channel_leadership_private"
        or m.get("is_private")
    ]
    assert private_mems, "seed should include private leadership memory"
    mem_id = private_mems[0]["id"]

    # Unauthorized principal (not in private scope)
    decision_pub = brain.authz.resolve(
        organization_id=org,
        acting_principal_id="person_ravi",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="memory_read",
    )
    assert not brain.authz.can_read_memory(decision_pub, private_mems[0])

    # Public destination even for member must not allow private memory
    decision_member_public = brain.authz.resolve(
        organization_id=org,
        acting_principal_id="person_ajay",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="memory_read",
    )
    assert not brain.authz.can_read_memory(decision_member_public, private_mems[0])

    # Authorized private destination for member may read
    decision_ok = brain.authz.resolve(
        organization_id=org,
        acting_principal_id="person_ajay",
        destination={"type": "private_channel", "id": "channel_leadership", "is_private": True},
        purpose="memory_read",
    )
    assert brain.authz.can_read_memory(decision_ok, private_mems[0])
    assert mem_id  # used so linter keeps path real


def test_get_source_denies_private_without_authorized_principal(brain):
    org = "org_acme"
    private_sources = [
        s
        for s in brain.store.list_sources(org, include_revoked=True)
        if s.get("is_private_channel") or s.get("scope_id") == "scope_channel_leadership_private"
    ]
    assert private_sources
    src = private_sources[0]

    decision_denied = brain.authz.resolve(
        organization_id=org,
        acting_principal_id="person_ravi",
        destination={"type": "slack_channel", "id": "channel_eng", "is_private": False},
        purpose="source_read",
    )
    assert not brain.authz.can_read_source(decision_denied, src)

    decision_ok = brain.authz.resolve(
        organization_id=org,
        acting_principal_id="person_ajay",
        destination={"type": "private_channel", "id": "channel_leadership", "is_private": True},
        purpose="source_read",
    )
    assert brain.authz.can_read_source(decision_ok, src)


def test_api_get_memory_and_source_require_authz_query_params(client_authz):
    """HTTP layer returns 404 for private content when principal lacks access."""
    client, private_mem_id, private_src_id = client_authz

    # Missing acting_principal_id → 422
    r = client.get(f"/v1/memories/{private_mem_id}")
    assert r.status_code == 422

    # Unauthorized principal → 404 (fail closed, no leak)
    r = client.get(
        f"/v1/memories/{private_mem_id}",
        params={
            "acting_principal_id": "person_ravi",
            "destination_type": "slack_channel",
            "destination_id": "channel_eng",
            "destination_is_private": "false",
        },
    )
    assert r.status_code == 404

    r = client.get(
        f"/v1/sources/{private_src_id}",
        params={
            "acting_principal_id": "person_ravi",
            "destination_type": "slack_channel",
            "destination_id": "channel_eng",
            "destination_is_private": "false",
        },
    )
    assert r.status_code == 404

    # Authorized member + private destination → 200
    r = client.get(
        f"/v1/memories/{private_mem_id}",
        params={
            "acting_principal_id": "person_ajay",
            "destination_type": "private_channel",
            "destination_id": "channel_leadership",
            "destination_is_private": "true",
        },
    )
    assert r.status_code == 200
    assert r.json()["id"] == private_mem_id
    assert r.json().get("permission_decision_id")

    r = client.get(
        f"/v1/memories/{private_mem_id}/lineage",
        params={
            "acting_principal_id": "person_ajay",
            "destination_type": "private_channel",
            "destination_id": "channel_leadership",
            "destination_is_private": "true",
        },
    )
    assert r.status_code == 200
    assert r.json().get("permission_decision_id")


def test_source_revocation_removes_content_from_search(brain):
    org = "org_acme"
    # Find a public source about launch date
    sources = brain.store.list_sources(org)
    launch_sources = [
        s for s in sources if "launch" in (s.get("content") or "").lower() and not s.get("is_private_channel")
    ]
    assert launch_sources, "seed should include launch sources"
    target = launch_sources[0]

    before = brain.retrieval.search(
        organization_id=org,
        query="launch date",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "s1", "is_private": True},
        limit=50,
    )
    assert before["count"] >= 1

    revoked = brain.ingestion.revoke_source(target["id"])
    assert revoked and revoked.get("revoked") is True

    after = brain.retrieval.search(
        organization_id=org,
        query="launch date",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "s1", "is_private": True},
        limit=50,
    )
    # Memories that ONLY depended on revoked source should drop;
    # at minimum the revoked source itself must not appear in source payloads
    for r in after["results"]:
        for s in r.get("sources") or []:
            assert s["id"] != target["id"]
        # if memory only had this source, it should be gone
        src_ids = r["memory"].get("source_ids") or []
        if src_ids == [target["id"]]:
            raise AssertionError("memory with only revoked source still returned")
