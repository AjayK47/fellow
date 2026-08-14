"""Concentrate speaks a Responses API, not OpenAI chat completions.

The differences are structural, so this covers the translation in both
directions: Toda's chat-shaped messages going out, and Concentrate's typed
output items coming back.
"""

from __future__ import annotations

from typing import Any

from toda.harness.model_client import ConcentrateResponsesModelClient
from toda.providers import ProviderConnection, ModelAssignment, ProviderResolver


def _client(**kw):
    return ConcentrateResponsesModelClient(
        api_key="k", model="gpt-5.6-sol", base_url="https://api.concentrate.ai/v1", **kw
    )


def _capture(monkeypatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def post(url, **kwargs):
        sent.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("toda.harness.model_client.httpx.post", post)
    return sent


_OK = {"status": "completed", "model": "gpt-5.6-sol",
       "output": [{"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "hello"}]}],
       "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}


def test_it_posts_to_responses_with_bearer_auth(monkeypatch):
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[{"role": "user", "content": "hi"}])
    assert sent[0]["url"] == "https://api.concentrate.ai/v1/responses"
    assert sent[0]["headers"]["Authorization"] == "Bearer k"


def test_the_system_prompt_becomes_instructions(monkeypatch):
    """There is no role "system" in the input list."""
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[
        {"role": "system", "content": "You are Toda."},
        {"role": "user", "content": "hi"},
    ])
    body = sent[0]["json"]
    assert body["instructions"] == "You are Toda."
    assert all(item.get("role") != "system" for item in body["input"])
    assert body["input"] == [{"role": "user", "content": "hi"}]


def test_tool_schemas_are_flattened(monkeypatch):
    """Concentrate declares a tool flat; OpenAI nests it under "function"."""
    sent = _capture(monkeypatch, _OK)
    _client().generate(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {
            "name": "web_search", "description": "Search.",
            "parameters": {"type": "object", "properties": {}}}}],
    )
    tool = sent[0]["json"]["tools"][0]
    assert tool == {"type": "function", "name": "web_search",
                    "description": "Search.",
                    "parameters": {"type": "object", "properties": {}}}
    assert "function" not in tool


def test_an_assistant_tool_call_turn_expands_into_items(monkeypatch):
    """The Responses model has no message that also carries calls, so one
    assistant turn becomes a message item plus one function_call item."""
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[
        {"role": "user", "content": "look it up"},
        {"role": "assistant", "content": "On it.", "tool_calls": [
            {"id": "call_1", "function": {"name": "web_search",
                                          "arguments": '{"q":"x"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "results"},
    ])
    items = sent[0]["json"]["input"]
    assert items[1] == {"role": "assistant", "content": "On it."}
    assert items[2] == {"type": "function_call", "call_id": "call_1",
                        "name": "web_search", "arguments": '{"q":"x"}'}
    # A tool result is an output item, not a message with role "tool".
    assert items[3] == {"type": "function_call_output", "call_id": "call_1",
                        "output": "results"}


def test_output_items_are_parsed_back_into_the_chat_contract(monkeypatch):
    """Everything above the transport keeps speaking Toda's shape."""
    _capture(monkeypatch, {
        "status": "completed", "model": "gpt-5.6-sol",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "the answer"}]},
            {"type": "function_call", "call_id": "c1", "name": "search",
             "arguments": '{"q":"toda"}'},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    })
    result = _client().generate(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "the answer"
    assert result.reasoning == "thinking"
    assert result.tool_calls[0].name == "search"
    assert result.tool_calls[0].arguments == {"q": "toda"}
    assert result.tool_calls[0].id == "c1"
    # usage is renamed so budgeting and the token guards keep working
    assert result.usage["prompt_tokens"] == 100
    assert result.usage["completion_tokens"] == 20
    assert result.finish_reason == "tool_calls"


def test_incomplete_status_maps_to_length(monkeypatch):
    """So the recovery path reads it as budget exhaustion."""
    _capture(monkeypatch, {"status": "incomplete", "output": [],
                           "usage": {"input_tokens": 1, "output_tokens": 0}})
    result = _client().generate(messages=[{"role": "user", "content": "hi"}])
    assert result.finish_reason == "length"


def test_an_error_payload_raises_with_the_body(monkeypatch):
    _capture(monkeypatch, {"error": {"code": "bad_model", "message": "nope"}})
    try:
        _client().generate(messages=[{"role": "user", "content": "hi"}])
    except RuntimeError as exc:
        assert "bad_model" in str(exc)
    else:
        raise AssertionError("an error payload must raise")


def test_reasoning_effort_and_suppression(monkeypatch):
    sent = _capture(monkeypatch, _OK)
    _client(reasoning_effort="medium").generate(messages=[{"role": "user", "content": "hi"}])
    assert sent[0]["json"]["reasoning"] == {"effort": "medium"}

    # reasoning=False omits the field rather than sending effort "none":
    # Concentrate maps that to MINIMAL and ai-studio rejects it with
    # "Thinking level MINIMAL is not supported for this model."
    sent2 = _capture(monkeypatch, _OK)
    _client(reasoning_effort="medium").generate(
        messages=[{"role": "user", "content": "hi"}], reasoning=False
    )
    assert "reasoning" not in sent2[0]["json"]


def test_max_tokens_uses_the_responses_field(monkeypatch):
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[{"role": "user", "content": "hi"}], max_tokens=4096)
    assert sent[0]["json"]["max_output_tokens"] == 4096
    assert "max_tokens" not in sent[0]["json"]


def test_the_provider_resolves_through_the_registry():
    """A config naming provider "concentrate" builds this client."""
    from toda.harness.model_client import _build_resolved_model
    from toda.config import Settings

    resolver = ProviderResolver(
        {"c": ProviderConnection(provider="concentrate", api_key="k")},
        {"main": ModelAssignment(connection="c", model="auto")},
    )
    resolved = resolver.resolve("main")
    assert resolved is not None
    client = _build_resolved_model(resolved, Settings())
    assert isinstance(client, ConcentrateResponsesModelClient)
    assert client.base_url == "https://api.concentrate.ai/v1"


def test_embeddings_refuse_a_non_chat_provider():
    """Concentrate has no embeddings endpoint, so an embedding role must
    point elsewhere -- OpenRouter keeps serving them."""
    from toda.providers import providers

    profile = providers.get("concentrate")
    assert profile.api_mode == "concentrate_responses"
    assert profile.supports_embeddings is False


def test_a_trailing_model_turn_gets_a_synthetic_user_turn(monkeypatch):
    """Gemini via ai-studio rejects the request outright:

        400 invalid_prompt -- Requests ending with a model turn are not
        supported.

    Toda hits this because build_agent_messages replays session history
    verbatim without appending the goal as a closing user turn, so the tail
    is whatever was last stored -- often Toda's own previous reply.
    """
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! How can I help?"},
    ])
    items = sent[0]["json"]["input"]
    assert items[-1] == {"role": "user", "content": "Respond to the most recent request above."}
    # Appended, never trimmed: the latest context is what the model needs.
    assert items[-2] == {"role": "assistant", "content": "Hey! How can I help?"}


def test_a_trailing_function_call_also_counts_as_a_model_turn(monkeypatch):
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
    ])
    items = sent[0]["json"]["input"]
    assert items[-1]["role"] == "user"
    assert items[-2]["type"] == "function_call"


def test_a_tool_result_tail_is_left_alone(monkeypatch):
    """function_call_output is not a model turn -- the provider accepts it,
    and appending here would interrupt a live tool loop."""
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out"},
    ])
    assert sent[0]["json"]["input"][-1]["type"] == "function_call_output"


def test_a_normal_user_tail_is_untouched(monkeypatch):
    sent = _capture(monkeypatch, _OK)
    _client().generate(messages=[
        {"role": "assistant", "content": "prev"},
        {"role": "user", "content": "new question"},
    ])
    items = sent[0]["json"]["input"]
    assert len(items) == 2
    assert items[-1] == {"role": "user", "content": "new question"}
