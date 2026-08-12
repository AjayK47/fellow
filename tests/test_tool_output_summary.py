"""H-14: oversized tool output gets summarized (or, with no live summarizer,
deterministically truncated) instead of chopped raw — and the full result is
always retrievable as an artifact."""

from __future__ import annotations

import json

from toda.harness.model_client import DeterministicHarnessModel, ScriptedModelClient
from toda.harness.models import ModelResponse, ModelToolCall, ToolDefinition
from toda.harness.tool_output_summary import ToolOutputSummarizer


def _big_serialized(n: int = 5000) -> str:
    return json.dumps({"items": [f"row-{i}" for i in range(n)]})


def test_summarizer_is_not_live_for_the_deterministic_model():
    summarizer = ToolOutputSummarizer(model=DeterministicHarnessModel())
    assert summarizer.live is False


def test_summarizer_is_live_for_a_configured_model():
    summarizer = ToolOutputSummarizer(model=ScriptedModelClient([ModelResponse(content="{}")]))
    assert summarizer.live is True


def test_non_live_summarizer_falls_back_to_deterministic_truncation():
    summarizer = ToolOutputSummarizer(model=DeterministicHarnessModel())
    serialized = _big_serialized()
    result = summarizer.summarize(tool_name="brain_search", serialized=serialized)
    assert result["truncated"] is True
    assert result["original_characters"] == len(serialized)
    assert len(result["content"]) <= 20_000
    # Must always be JSON-serializable on its own.
    json.dumps(result)


def test_live_summarizer_produces_a_structured_summary():
    payload = {
        "summary": "3 rows describe repeated export failures for Acme.",
        "item_count": 3,
        "key_points": ["export job failed", "retried twice", "customer notified"],
    }
    model = ScriptedModelClient([ModelResponse(content=json.dumps(payload))])
    summarizer = ToolOutputSummarizer(model=model)
    serialized = _big_serialized()
    result = summarizer.summarize(tool_name="brain_search", serialized=serialized)
    assert result["summarized"] is True
    assert result["summary"] == payload["summary"]
    assert result["key_points"] == payload["key_points"]
    assert result["original_characters"] == len(serialized)


def test_live_summarizer_falls_back_on_malformed_model_output():
    model = ScriptedModelClient([ModelResponse(content="not valid json")])
    summarizer = ToolOutputSummarizer(model=model)
    serialized = _big_serialized()
    result = summarizer.summarize(tool_name="brain_search", serialized=serialized)
    assert result["truncated"] is True


def test_bounded_output_creates_a_retrievable_artifact_with_the_full_result(brain):
    large_output = {"items": [f"row-{i}" for i in range(5000)]}
    run = {
        "id": "arun_test_bounded",
        "organization_id": "org_acme",
        "envelope": {
            "principal_id": "person_ajay",
            "destination": {"type": "slack_channel", "id": "channel_eng", "is_private": False},
        },
    }
    call = ModelToolCall(id="call_big", name="brain_search", arguments={"query": "x"})
    definition = ToolDefinition(
        name="brain_search",
        description="test",
        input_schema={"type": "object", "properties": {}},
        risk="read",
    )

    bounded = brain.tool_executor._bounded_output(
        large_output, run=run, call=call, definition=definition
    )
    assert "artifact_id" in bounded
    json.dumps(bounded)  # must always be valid JSON on its own

    artifact = brain.artifacts.get(bounded["artifact_id"])
    assert artifact is not None
    assert artifact["content"] == large_output


def test_bounded_output_leaves_small_results_untouched(brain):
    small_output = {"ok": True, "count": 1}
    run = {
        "id": "arun_test_small",
        "organization_id": "org_acme",
        "envelope": {
            "principal_id": "person_ajay",
            "destination": {"type": "slack_channel", "id": "channel_eng", "is_private": False},
        },
    }
    call = ModelToolCall(id="call_small", name="brain_search", arguments={"query": "x"})
    definition = ToolDefinition(
        name="brain_search",
        description="test",
        input_schema={"type": "object", "properties": {}},
        risk="read",
    )
    bounded = brain.tool_executor._bounded_output(
        small_output, run=run, call=call, definition=definition
    )
    assert bounded == small_output
