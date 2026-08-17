"""One inbound message must produce one trace, not one per queue hop.

Toda is queue-driven: a Slack message becomes an `event.route` job, which
enqueues `session.dispatch`, which runs the agent, which enqueues delivery.
Every hop is a different thread picking work off the database, and Python
context does not cross that boundary by itself.

The first version of tracing read `payload.get("trace_id")` on the way out
of the queue while nothing ever put one in, so the lookup was always None
and every hop invented a fresh id. The data was all there and none of it
was connected -- which is the same as having no traces at all, because the
question anyone actually asks is "what happened when I sent that message".
"""

from __future__ import annotations

import json

from toda.observability import current_trace_id, trace_context


def _mention(brain, *, text: str = "Hey <@UBOT>", event_id: str = "EvTrace") -> dict:
    brain.slack_gateway.config.slack.bot_user_id = "UBOT"
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": event_id,
        "event": {
            "type": "app_mention",
            "user": "UAJAY",
            "channel": "CSOCIAL",
            "text": text,
            "ts": "700.001",
            "client_msg_id": event_id.lower(),
        },
    }
    return brain.slack_gateway.accept(
        body=json.dumps(payload).encode(), headers={}, allow_unsigned=True
    )


def test_one_slack_message_produces_one_connected_trace(brain):
    accepted = _mention(brain)
    assert accepted["accepted"]
    trace_id = accepted["trace_id"]
    assert brain.harness_worker.drain_until_idle()["failed"] == 0

    events = brain.store.list_trace_events(trace_id, organization_id="org_acme")
    assert events, "the inbound message recorded no trace at all"

    # The hops that matter, each on the far side of a queue boundary from
    # the one before it.
    operations = {event["operation"] for event in events}
    assert "slack.event.accepted" in operations
    assert "event.route" in operations, "routing started its own trace"
    assert "session.dispatch" in operations, "dispatch started its own trace"

    # And every one of them is genuinely on this trace, not merely present.
    assert {event["trace_id"] for event in events} == {trace_id}


def test_the_trace_carries_across_the_queue_on_its_own(brain):
    """Nothing passes trace_id by hand: enqueue() stamps whatever is in
    force. If that regressed, every caller would have to remember, and the
    previous attempt shows how that ends."""
    with trace_context(trace_id="trace_carried"):
        job = brain.harness_jobs.enqueue(
            organization_id="org_acme",
            kind="delivery.status_update",
            payload={"task_id": "task_none"},
            idempotency_key="trace-carry-test",
        )
    assert job["payload"]["trace_id"] == "trace_carried"


def test_an_explicit_trace_id_in_a_payload_is_not_overwritten(brain):
    with trace_context(trace_id="trace_ambient"):
        job = brain.harness_jobs.enqueue(
            organization_id="org_acme",
            kind="delivery.status_update",
            payload={"task_id": "task_none", "trace_id": "trace_explicit"},
            idempotency_key="trace-explicit-test",
        )
    assert job["payload"]["trace_id"] == "trace_explicit"


def test_context_is_restored_after_the_block(brain):
    assert current_trace_id() is None
    with trace_context(trace_id="trace_inner"):
        assert current_trace_id() == "trace_inner"
    assert current_trace_id() is None


def test_spans_nest_under_the_span_that_opened_them(brain):
    with trace_context(trace_id="trace_nested", organization_id="org_acme"):
        with brain.store.trace.span("outer", component="test"):
            with brain.store.trace.span("inner", component="test"):
                pass

    events = brain.store.list_trace_events("trace_nested", organization_id="org_acme")
    outer = next(e for e in events if e["operation"] == "outer")
    inner = next(e for e in events if e["operation"] == "inner")
    assert inner["parent_span_id"] == outer["span_id"]
    assert inner["span_id"] != outer["span_id"]


def test_a_caught_failure_still_reads_as_failed(brain):
    """A tool that fails returns ToolResult(status="failed") rather than
    raising, so without span.error() the one view meant to show what went
    wrong would record it as a success."""
    with trace_context(trace_id="trace_softfail", organization_id="org_acme"):
        with brain.store.trace.span("soft", component="test") as span:
            span.error({"type": "ValueError", "message": "nope"})

    events = brain.store.list_trace_events("trace_softfail", organization_id="org_acme")
    assert events[-1]["phase"] == "failed"
    assert events[-1]["error"]["message"] == "nope"


def test_payload_capture_can_actually_be_turned_off(brain):
    """The switch someone reaches for when they do not want tool arguments
    on disk. It was wired to nothing."""
    brain.store.trace.capture_payloads = False
    try:
        with trace_context(trace_id="trace_nopayload", organization_id="org_acme"):
            brain.store.trace.record(
                "sensitive", component="test", phase="completed",
                input={"customer_email": "someone@example.com"},
                output={"rows": ["secret"]},
            )
    finally:
        brain.store.trace.capture_payloads = True

    event = brain.store.list_trace_events(
        "trace_nopayload", organization_id="org_acme"
    )[0]
    assert "someone@example.com" not in json.dumps(event)
    # Context survives, because it is what keeps the trace navigable.
    assert event["operation"] == "sensitive"


def test_traces_are_listed_per_organization(brain):
    with trace_context(trace_id="trace_listed", organization_id="org_acme"):
        with brain.store.trace.span("listed.root", component="test"):
            pass
    with trace_context(trace_id="trace_other", organization_id="org_other"):
        with brain.store.trace.span("other.root", component="test"):
            pass

    listed = brain.store.list_traces("org_acme")
    ids = {row["trace_id"] for row in listed}
    assert "trace_listed" in ids
    assert "trace_other" not in ids, "traces leaked across organizations"
    row = next(row for row in listed if row["trace_id"] == "trace_listed")
    assert row["root_operation"] == "listed.root"
    assert row["failures"] == 0


# ---- the viewer's HTTP surface ----


def _seed_trace(brain, trace_id: str = "trace_http") -> None:
    with trace_context(trace_id=trace_id, organization_id="org_acme"):
        with brain.store.trace.span("http.root", component="test"):
            with brain.store.trace.span("http.child", component="test") as child:
                child.error({"type": "RuntimeError", "message": "inner boom"})


def test_the_trace_route_returns_a_nested_tree(api_client):
    from tests.conftest import sign_in

    sign_in(api_client)
    _seed_trace(api_client.app.state.brain)

    response = api_client.get(
        "/v1/traces/trace_http", params={"organization_id": "org_acme"}
    )
    assert response.status_code == 200, response.text
    tree = response.json()["tree"]

    # One root with one child, not two flat rows: the pairing of `started`
    # and `completed` into a single span is what makes the view readable.
    assert len(tree) == 1
    assert tree[0]["operation"] == "http.root"
    assert tree[0]["duration_ms"] is not None
    assert [child["operation"] for child in tree[0]["children"]] == ["http.child"]
    assert tree[0]["children"][0]["phase"] == "failed"


def test_the_trace_list_shows_failures(api_client):
    from tests.conftest import sign_in

    sign_in(api_client)
    _seed_trace(api_client.app.state.brain, trace_id="trace_http_list")

    listed = api_client.get("/v1/traces", params={"organization_id": "org_acme"})
    assert listed.status_code == 200, listed.text
    row = next(
        item for item in listed.json()["traces"] if item["trace_id"] == "trace_http_list"
    )
    assert row["failures"] == 1

    only_failures = api_client.get(
        "/v1/traces", params={"organization_id": "org_acme", "errors_only": True}
    )
    assert "trace_http_list" in {
        item["trace_id"] for item in only_failures.json()["traces"]
    }


def test_a_member_cannot_read_traces(api_client):
    """A trace spans whatever conversation caused it, so the list would
    otherwise be a way to read every room in the workspace without being a
    participant in any of them."""
    from tests.conftest import sign_in

    _seed_trace(api_client.app.state.brain, trace_id="trace_http_member")
    sign_in(api_client, principal_id="person_ravi", role="member")

    assert api_client.get(
        "/v1/traces", params={"organization_id": "org_acme"}
    ).status_code == 403
    assert api_client.get(
        "/v1/traces/trace_http_member", params={"organization_id": "org_acme"}
    ).status_code == 403


def test_one_organization_cannot_read_anothers_trace(api_client):
    from tests.conftest import sign_in

    sign_in(api_client)
    brain = api_client.app.state.brain
    with trace_context(trace_id="trace_foreign", organization_id="org_other"):
        with brain.store.trace.span("foreign.root", component="test"):
            pass

    response = api_client.get(
        "/v1/traces/trace_foreign", params={"organization_id": "org_acme"}
    )
    assert response.status_code == 404
