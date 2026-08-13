"""Profile projections must remain permission-filtered at read time."""

from __future__ import annotations


def test_profile_refresh_and_safe_read(api_client):
    refreshed = api_client.post(
        "/v1/profiles/project_toda:refresh", json={"organization_id": "org_acme"}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["profile_type"] == "project"

    public = api_client.get(
        "/v1/profiles/project_toda",
        params={
            "organization_id": "org_acme", "acting_principal_id": "person_ajay",
            "destination_type": "slack_channel", "destination_id": "channel_eng",
            "destination_is_private": "false",
        },
    )
    assert public.status_code == 200
    assert public.json()["permission_decision_id"]
    assert public.json()["profile"]["entity_id"] == "project_toda"


def test_customer_profile_hides_private_memories_in_public_destination(api_client):
    response = api_client.get(
        "/v1/profiles/person_ajay",
        params={
            "organization_id": "org_acme", "acting_principal_id": "person_ajay",
            "destination_type": "slack_channel", "destination_id": "channel_eng",
            "destination_is_private": "false",
        },
    )
    assert response.status_code == 200
    rendered = str(response.json()).lower()
    assert "headcount freeze" not in rendered
    assert "confidential" not in rendered
