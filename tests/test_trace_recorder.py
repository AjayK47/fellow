
def test_trace_recorder_persists_redacted_nested_payloads(brain):
    with brain.store.trace.span(
        "test.operation",
        component="test",
        input={"Authorization": "Bearer secret", "value": "hello"},
        task_id="task_trace",
    ) as span:
        span.output({"api_key": "hidden", "count": 2})

    # Recover the trace id from the persisted event rather than relying on
    # implementation details of ContextVar tokens.
    with brain.store.cursor() as cur:
        cur.execute("SELECT trace_id FROM trace_events ORDER BY created_at DESC LIMIT 1")
        trace_id = cur.fetchone()["trace_id"]
    events = brain.store.list_trace_events(trace_id)
    assert [event["phase"] for event in events] == ["started", "completed"]
    assert events[0]["input"]["Authorization"] == "[REDACTED]"
    assert events[1]["output"]["api_key"] == "[REDACTED]"
    assert events[1]["duration_ms"] is not None


def test_trace_recorder_records_failures(brain):
    try:
        with brain.store.trace.span("test.failure", component="test"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with brain.store.cursor() as cur:
        cur.execute("SELECT trace_id FROM trace_events WHERE operation = 'test.failure' ORDER BY created_at DESC LIMIT 1")
        trace_id = cur.fetchone()["trace_id"]
    events = brain.store.list_trace_events(trace_id)
    assert events[-1]["phase"] == "failed"
    assert events[-1]["error"]["message"] == "boom"
