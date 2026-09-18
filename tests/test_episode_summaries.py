"""M6: real episode summaries via the shared compactor."""

import pytest


@pytest.fixture(autouse=True)
def _no_quiet_window(brain):
    brain.event_router.config.worker.episode_quiet_seconds = 0


class _ScriptedCompactor:
    def __init__(self):
        self.calls = []

    def compact(self, *, previous_summary, new_messages):
        self.calls.append((previous_summary, list(new_messages)))
        keep = previous_summary or ""
        # Keep the first event's decision line forever, append the latest.
        first_new = new_messages[0]["content"] if new_messages else ""
        if "DECISION" in first_new and "DECISION" not in keep:
            keep = (keep + "\n" + first_new).strip()
        latest = new_messages[-1]["content"] if new_messages else ""
        return (keep + "\n" + latest).strip()[-4000:]


def test_long_thread_keeps_first_decision_in_final_summary(brain):
    brain.harness_memory.history_compactor = _ScriptedCompactor()
    group_key = "slack:TACME:Clong:1"
    for i in range(150):
        text = f"filler chatter {i}"
        if i == 0:
            text = "DECISION: we will use Postgres for billing"
        brain.event_router.route(
            {
                "id": f"ev_long_{i}",
                "organization_id": "org_acme",
                "source": "slack",
                "type": "slack.channel_message",
                "actor": {"id": "person_ajay", "type": "person"},
                "scope": {
                    "workspace_id": "TACME",
                    "channel_id": "Clong",
                    "visibility": "organization_public",
                    "scope_id": "scope_org_public",
                },
                "payload": {"text": text, "conversation_key": group_key},
                "dedupe_key": f"dedupe-ev_long_{i}",
            }
        )
    drained = brain.harness_worker.drain_until_idle(
        kinds=brain.harness_worker.knowledge_job_kinds()
    )
    assert drained["failed"] == 0, drained
    episode = brain.store.get_episode_by_group_key("org_acme", group_key)
    assert episode is not None
    assert "DECISION" in (episode["summary"] or "")
    assert "Postgres" in (episode["summary"] or "")
