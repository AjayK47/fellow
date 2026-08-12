import hashlib
import hmac

from toda.adapters import (
    normalize_composio_trigger,
    normalize_slack_event,
    verify_slack_signature,
)


def test_slack_signature_and_event_normalization() -> None:
    body = b'{"type":"event_callback"}'
    timestamp = "1700000000"
    expected = "v0=" + hmac.new(b"secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature(signing_secret="secret", timestamp=timestamp, raw_body=body, signature=expected, now=1700000001)
    assert not verify_slack_signature(signing_secret="secret", timestamp=timestamp, raw_body=body, signature=expected, now=1700000401)
    event = normalize_slack_event({"type": "event_callback", "team_id": "T1", "event_id": "Ev1", "event": {"type": "message", "user": "person_a", "channel": "C1", "text": "Ship date is October 1"}}, organization_id="org", scope_id="scope")
    assert event["source"] == "slack:T1"
    assert event["data"]["text"] == "Ship date is October 1"


def test_composio_v3_normalization() -> None:
    event = normalize_composio_trigger({"id": "msg_1", "type": "composio.trigger.message", "metadata": {"trigger_slug": "GITHUB_COMMIT_EVENT", "trigger_id": "ti_1", "connected_account_id": "ca_1", "user_id": "person_a"}, "data": {"message": "fix rollback"}}, organization_id="org", scope_id="scope")
    assert event["source"] == "composio:ca_1"
    assert event["type"] == "composio.github_commit_event"
    assert event["data"]["content"] == "fix rollback"
