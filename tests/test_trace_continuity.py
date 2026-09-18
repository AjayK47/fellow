from __future__ import annotations

import json

from toda.telemetry.context import carrier_has_context


def _mention(brain, *, event_id: str = "EvTrace") -> dict:
    brain.slack_gateway.config.slack.bot_user_id = "UBOT"
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": event_id,
        "event": {
            "type": "app_mention", "user": "UAJAY", "channel": "CSOCIAL",
            "text": "Hey <@UBOT>", "ts": "700.001", "client_msg_id": event_id.lower(),
        },
    }
    return brain.slack_gateway.accept(
        body=json.dumps(payload).encode(), headers={}, allow_unsigned=True
    )


def test_one_slack_message_produces_connected_otel_spans(brain):
    accepted = _mention(brain)
    assert accepted["accepted"]
    assert brain.harness_worker.drain_until_idle()["failed"] == 0
    traces = brain.store.list_otel_traces("org_acme", limit=20)
    assert traces
    trace_ids = {item["trace_id"] for item in traces}
    rows = [
        span
        for trace_id in trace_ids
        for span in brain.store.list_otel_spans(trace_id, organization_id="org_acme")
    ]
    names = {span["name"] for span in rows}
    assert "channel.receive" in names
    assert "queue.publish" in names
    assert "queue.process" in names


def test_queue_carrier_is_transport_metadata_and_preserves_trace(brain):
    queue = brain.harness_jobs
    with brain.store.telemetry.span("root", organization_id="org_acme") as root:
        job = queue.enqueue(
            organization_id="org_acme", kind="delivery.acknowledge",
            payload={"task_id": "task_none"}, idempotency_key="trace-carry-test",
        )
        trace_id = f"{root.span.get_span_context().trace_id:032x}"
    assert "trace_id" not in job["payload"]
    assert carrier_has_context(job["telemetry"])
    assert any(row["name"] == "queue.publish" for row in brain.store.list_otel_spans(trace_id, organization_id="org_acme"))


def test_spans_nest_and_failures_are_terminal(brain):
    with brain.store.telemetry.span("outer", organization_id="org_acme") as outer:
        trace_id = f"{outer.span.get_span_context().trace_id:032x}"
        with brain.store.telemetry.span("inner", organization_id="org_acme") as inner:
            inner.error({"type": "ValueError", "message": "nope"})
    rows = brain.store.list_otel_spans(trace_id, organization_id="org_acme")
    outer_row = next(row for row in rows if row["name"] == "outer")
    inner_row = next(row for row in rows if row["name"] == "inner")
    assert inner_row["parent_span_id"] == outer_row["span_id"]
    assert inner_row["status_code"] == "ERROR"


def test_traces_are_tenant_scoped(brain):
    with brain.store.telemetry.span("listed.root", organization_id="org_acme") as span:
        trace_id = f"{span.span.get_span_context().trace_id:032x}"
    with brain.store.telemetry.span("other.root", organization_id="org_other"):
        pass
    listed = brain.store.list_otel_traces("org_acme")
    assert trace_id in {row["trace_id"] for row in listed}
    assert all(row["trace_id"] != trace_id or row["root_operation"] == "listed.root" for row in listed)


def test_trace_route_returns_a_nested_tree(api_client):
    from tests.conftest import sign_in

    sign_in(api_client)
    brain = api_client.app.state.brain
    with brain.store.telemetry.span("http.root", organization_id="org_acme") as root:
        trace_id = f"{root.span.get_span_context().trace_id:032x}"
        with brain.store.telemetry.span("http.child", organization_id="org_acme"):
            pass
    response = api_client.get(f"/v1/traces/{trace_id}", params={"organization_id": "org_acme"})
    assert response.status_code == 200, response.text
    tree = response.json()["tree"]
    assert len(tree) == 1
    assert tree[0]["name"] == "http.root"
    assert tree[0]["children"][0]["name"] == "http.child"
