"""Secret/credential redaction: the module itself, and its wiring into
IngestionService.ingest() — the one funnel all raw content, post-run memory
capture, and episode consolidation flow through."""

from __future__ import annotations

from docket.services.redaction import redact, scan


def test_redact_replaces_aws_access_key():
    result = redact("Here's the key: AKIAABCDEFGHIJKLMNOP for the demo account.")
    assert "AKIAABCDEFGHIJKLMNOP" not in result.text
    assert "[REDACTED:aws_access_key]" in result.text
    assert result.categories == ["aws_access_key"]


def test_redact_replaces_github_token():
    token = "ghp_" + "a" * 36
    result = redact(f"use this token: {token}")
    assert token not in result.text
    assert result.categories == ["github_token"]


def test_redact_replaces_private_key_block():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAK...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact(f"deploy key:\n{block}\nplease rotate")
    assert "MIIBOgIBAAJBAK" not in result.text
    assert result.categories == ["private_key_block"]


def test_redact_replaces_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fw"
    result = redact(f"session token is {jwt}")
    assert jwt not in result.text
    assert "jwt" in result.categories


def test_redact_replaces_bearer_header():
    result = redact("Authorization: Bearer sk-test-abcdefghijklmnopqrstuvwx")
    assert "sk-test-abcdefghijklmnopqrstuvwx" not in result.text
    assert "bearer_token" in result.categories


def test_redact_replaces_connection_string_credential():
    result = redact("connect via postgres://admin:hunter2pass@db.internal:5432/prod")
    assert "hunter2pass" not in result.text
    assert "connection_string_credential" in result.categories


def test_redact_replaces_generic_key_value_secret():
    result = redact("password: SuperSecret123!")
    assert "SuperSecret123!" not in result.text
    assert "key_value_secret" in result.categories


def test_redact_leaves_ordinary_text_untouched():
    text = "Let's ship the export fix and update the customer by Friday."
    result = redact(text)
    assert result.text == text
    assert result.categories == []
    assert not result.redacted


def test_redact_handles_multiple_secrets_in_one_message():
    text = (
        "rotate AKIAABCDEFGHIJKLMNOP and also password: hunter2xyz please"
    )
    result = redact(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in result.text
    assert "hunter2xyz" not in result.text
    assert set(result.categories) == {"aws_access_key", "key_value_secret"}


def test_scan_reports_categories_without_mutating_text():
    text = "key: AKIAABCDEFGHIJKLMNOP"
    categories = scan(text)
    assert categories == ["aws_access_key"]


def test_ingest_redacts_secret_before_storing_source(brain):
    secret = "AKIAABCDEFGHIJKLMNOP"
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="ev_secret_1",
        content=f"heads up, rotate this key: {secret} before EOD",
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        learn=False,
        processing_mode="reference",
    )
    stored = brain.store.get_source(result["source"]["id"])
    assert secret not in stored["content"]
    assert "[REDACTED:aws_access_key]" in stored["content"]

    audited = brain.store.list_audit("org_acme", event_type="ingestion.secret_redacted")
    matches = [item for item in audited if item["payload"]["source_id"] == result["source"]["id"]]
    assert matches, "redaction must be audited"
    assert matches[0]["payload"]["categories"] == ["aws_access_key"]
    assert secret not in str(matches[0])


def test_ingest_without_secret_never_emits_redaction_audit(brain):
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="slack.channel_message",
        external_id="ev_no_secret_1",
        content="the export job finished successfully",
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        learn=False,
        processing_mode="reference",
    )
    audited = brain.store.list_audit("org_acme", event_type="ingestion.secret_redacted")
    matches = [item for item in audited if item["payload"]["source_id"] == result["source"]["id"]]
    assert not matches
