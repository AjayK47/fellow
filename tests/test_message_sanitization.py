"""The pre-send chokepoint: reasoning echo direction and empty-turn repair.

Both rules exist because getting either wrong fails invisibly until a model
says something bizarre in a channel. Ported from Hermes's
agent/message_sanitization.py and repair_empty_non_final_messages.
"""

from __future__ import annotations

import json

from toda.harness.message_sanitization import (
    INTERRUPTED_PLACEHOLDER,
    needs_reasoning_echo,
    reasoning_echo_family,
    repair_empty_non_final_messages,
    sanitize_api_messages,
)


def test_deepseek_requires_echo_back_wherever_it_is_served():
    """Its thinking mode answers HTTP 400 without reasoning_content, so the
    model name is enough regardless of who serves it."""
    assert reasoning_echo_family("openrouter", "deepseek/deepseek-v4-flash-0731",
                                 "https://openrouter.ai/api/v1") == "deepseek"
    assert reasoning_echo_family("deepseek", "deepseek-chat",
                                 "https://api.deepseek.com") == "deepseek"


def test_kimi_is_host_driven_because_aggregators_reexport_it():
    """An aggregator re-exporting kimi speaks its own protocol and rejects
    reasoning_content echoes -- so kimi matches on host, never model name."""
    assert reasoning_echo_family("openrouter", "moonshotai/kimi-k2",
                                 "https://openrouter.ai/api/v1") is None
    assert reasoning_echo_family("kimi-coding", "k2",
                                 "https://api.kimi.com") == "kimi"


def test_strict_providers_are_on_the_no_echo_side():
    """Mistral/Cerebras/Groq reject any reasoning_content key with 400/422."""
    for provider, model, url in [
        ("groq", "llama-3.3-70b", "https://api.groq.com/openai/v1"),
        ("openai", "gpt-5", "https://api.openai.com/v1"),
    ]:
        assert not needs_reasoning_echo(provider, model, url)


def test_reasoning_is_stripped_for_strict_targets():
    out = sanitize_api_messages(
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": "ok", "reasoning": "thought",
          "reasoning_content": "thought", "reasoning_details": [{"a": 1}]}],
        provider="groq", model="llama-3.3-70b", base_url="https://api.groq.com/openai/v1",
    )
    assistant = out[-1]
    assert "reasoning" not in assistant
    assert "reasoning_content" not in assistant
    assert "reasoning_details" not in assistant


def test_reasoning_is_promoted_to_reasoning_content_for_echo_targets():
    """reasoning is the response field; reasoning_content is the request
    field. Sending the former as the latter is what produced a bare empty
    prefill and put the system prompt into Slack."""
    out = sanitize_api_messages(
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": "", "reasoning": "weighing it"}],
        provider="openrouter", model="deepseek/deepseek-v4-flash-0731",
        base_url="https://openrouter.ai/api/v1",
    )
    assert out[-1]["reasoning_content"] == "weighing it"
    assert "reasoning" not in out[-1]


def test_only_tool_call_messages_are_padded():
    """The requirement is reasoning_content on assistant TOOL-CALL messages.
    DeepSeek V4 Pro rejects the empty string, so those pad with a space."""
    out = sanitize_api_messages(
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]}],
        provider="deepseek", model="deepseek-chat", base_url="https://api.deepseek.com",
    )
    assert out[-1]["reasoning_content"] == " "


def test_a_plain_assistant_turn_is_never_padded():
    """Padding one describes a thinking turn whose thinking was blank.
    deepseek-v4-flash answers that with a single token and finish_reason
    "stop", which every layer above reads as "the model returned nothing"
    and fails the Task -- observed in production on a follow-up message
    where history held one ordinary assistant reply."""
    out = sanitize_api_messages(
        [{"role": "user", "content": "hey"},
         {"role": "assistant", "content": "Hey! What can I help you with today?"},
         {"role": "user", "content": "look up the latest news"}],
        provider="openrouter", model="deepseek/deepseek-v4-flash-0731",
        base_url="https://openrouter.ai/api/v1",
    )
    assert "reasoning_content" not in out[1]


def test_empty_non_final_messages_are_repaired():
    """The Anthropic schema rejects any request whose transcript contains an
    empty non-final message, and one poisons every later turn."""
    out = repair_empty_non_final_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "still there?"},
    ])
    assert out[1]["content"] == INTERRUPTED_PLACEHOLDER


def test_a_trailing_empty_assistant_is_left_alone():
    """That one is a deliberate prefill -- repairing it would defeat the
    continuation path entirely."""
    out = repair_empty_non_final_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "reasoning_content": "thinking"},
    ])
    assert out[-1]["content"] == ""


def test_a_tool_call_message_counts_as_non_empty():
    out = repair_empty_non_final_messages([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "content": "", "tool_call_id": "c"},
        {"role": "user", "content": "next"},
    ])
    assert out[0]["content"] == ""
    assert out[1]["content"] == ""


def test_schema_references_in_tool_output_are_neutralized():
    """A tool that returns a JSON Schema puts `$defs`/`$ref` into the
    transcript, and Gemini tries to *resolve* them instead of reading them
    as data:

        The referenced name `#/$defs/SearchContactsByCriteriaResponse` in
        function_response.response does not match to a display_name

    One `composio_describe` of a HubSpot action poisoned every later request
    in that Task with a permanent 400."""
    from toda.harness.message_sanitization import neutralize_schema_refs

    described = {
        "slug": "HUBSPOT_SEARCH_CONTACTS_BY_CRITERIA",
        "output_parameters": {
            "$defs": {"SearchContactsByCriteriaResponse": {"type": "object"}},
            "properties": {"data": {"$ref": "#/$defs/SearchContactsByCriteriaResponse"}},
        },
    }
    clean = neutralize_schema_refs(described)
    flat = json.dumps(clean)
    assert "$defs" not in flat and "$ref" not in flat
    # The information survives -- a schema stripped of its definitions would
    # actively mislead a model about to call the tool.
    assert "defs_" in flat and "ref_" in flat
    assert "SearchContactsByCriteriaResponse" in flat
    assert clean["slug"] == "HUBSPOT_SEARCH_CONTACTS_BY_CRITERIA"


def test_ordinary_tool_output_is_untouched():
    from toda.harness.message_sanitization import neutralize_schema_refs

    payload = {"contacts": [{"id": 1, "email": "a@b.com"}], "count": 1}
    assert neutralize_schema_refs(payload) == payload
