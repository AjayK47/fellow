def test_otel_span_persists_safe_metadata(brain):
    with brain.store.telemetry.span(
        "test.operation",
        component="test",
        input={"Authorization": "Bearer secret", "value": "hello"},
        organization_id="org_acme",
        task_id="task_trace",
    ) as span:
        span.output({"api_key": "hidden", "count": 2})
        trace_id = f"{span.span.get_span_context().trace_id:032x}"

    rows = brain.store.list_otel_spans(trace_id, organization_id="org_acme")
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "test.operation"
    assert row["task_id"] == "task_trace"
    assert row["end_time_ns"] is not None
    assert "hello" not in str(row)
    assert "hidden" not in str(row)
    assert row["attributes"]["toda.debug.payload_captured"] is False


def test_otel_span_records_failures(brain):
    try:
        with brain.store.telemetry.span("test.failure", organization_id="org_acme") as span:
            trace_id = f"{span.span.get_span_context().trace_id:032x}"
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    row = brain.store.list_otel_spans(trace_id, organization_id="org_acme")[0]
    assert row["status_code"] == "ERROR"
    assert row["events"][0]["name"] == "exception"


def test_outbound_httpx_does_not_leak_baggage_or_toda_identifiers(brain, monkeypatch):
    import httpx
    from toda.telemetry.context import inject_carrier, extract_context

    # Simulate incoming carrier with malicious / privacy baggage
    incoming_carrier = {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "baggage": "secret_user_marker=do_not_leak,org=acme",
    }
    extracted = extract_context(incoming_carrier)
    assert extracted is not None

    captured_headers: dict[str, str] = {}

    def mock_send(self, request, **kwargs):
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.Client, "send", mock_send)

    with brain.store.telemetry.span(
        "task.execute",
        organization_id="org_secret_id",
        task_id="task_secret_id",
        session_id="session_secret_id",
    ) as span:
        trace_id = f"{span.span.get_span_context().trace_id:032x}"
        outbound = inject_carrier()
        # Verify inject_carrier creates W3C trace context only, no baggage
        assert "baggage" not in outbound
        assert "traceparent" in outbound

        # Make mock HTTP call
        client = httpx.Client()
        client.get("https://api.thirdparty.com/v1/test", headers=outbound)

    assert "secret_user_marker" not in str(captured_headers)
    assert "org_secret_id" not in str(captured_headers)
    assert "task_secret_id" not in str(captured_headers)
    assert "baggage" not in captured_headers

    # Verify stored span in SQLite contains safe attributes only
    rows = brain.store.list_otel_spans(trace_id, organization_id="org_secret_id")
    span_str = str(rows[0])
    assert "secret_user_marker" not in span_str


def test_approval_resume_creates_linked_trace_segment(brain):
    from toda.telemetry.context import inject_carrier, link_from_carrier

    # First segment: request approval
    with brain.store.telemetry.span("approval.request", organization_id="org_acme") as initial_span:
        carrier = inject_carrier()
        first_trace_id = f"{initial_span.span.get_span_context().trace_id:032x}"

    assert carrier.get("traceparent") is not None
    link = link_from_carrier(carrier)
    assert link is not None

    # Second segment: resume after approval with link to prior trace
    with brain.store.telemetry.span(
        "approval.resume",
        organization_id="org_acme",
        links=[link],
    ) as resume_span:
        second_trace_id = f"{resume_span.span.get_span_context().trace_id:032x}"

    assert first_trace_id != second_trace_id
    rows = brain.store.list_otel_spans(second_trace_id, organization_id="org_acme")
    assert len(rows) == 1
    assert len(rows[0]["links"]) == 1
    assert rows[0]["links"][0]["trace_id"] == first_trace_id
