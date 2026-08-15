"""The single pre-send chokepoint for provider message shape.

Ported from Hermes's ``agent/message_sanitization.py`` and
``repair_empty_non_final_messages``. Two rules live here, and they exist
because getting either wrong produces failures that are invisible until a
model says something bizarre in a Slack channel:

1. **Reasoning echo direction.** DeepSeek/Kimi/MiMo thinking modes *require*
   ``reasoning_content`` to be passed back on assistant turns and answer
   HTTP 400 without it. Strict OpenAI-compatible providers (Mistral,
   Cerebras, Groq, …) reject the same key with 400/422 "Extra inputs are not
   permitted". The field must therefore be added or stripped per target,
   never sent blindly.

2. **Empty non-final messages.** An assistant turn with no content mid
   transcript is rejected outright by the Anthropic schema and the proxies
   in front of it ("all messages must have non-empty content except for the
   optional final assistant message"). A *trailing* empty assistant message
   is different: it is a deliberate prefill, and must be left alone.

Hermes learned the hard way that repairing at write time forks the concept
across call sites; both rules run here, once, immediately before the wire.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# Substituted for an empty non-final message that would otherwise make the
# provider reject the whole request. Matches Hermes's placeholder so a
# healed transcript reads the same in both systems.
INTERRUPTED_PLACEHOLDER = "[response interrupted]"

# (family, exact providers, model substrings, base_url hosts)
#
# Note kimi carries NO model substrings: aggregators that re-export
# kimi/moonshot models speak their own protocol and reject reasoning_content
# echoes, so that family is host-driven only. DeepSeek is matched on the
# model name too, because its thinking mode enforces echo-back wherever it
# is served from.
_REASONING_ECHO_RULES: tuple[tuple[str, frozenset[str], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "kimi",
        frozenset({"kimi-coding", "kimi-coding-cn"}),
        (),
        ("api.kimi.com", "moonshot.ai", "moonshot.cn"),
    ),
    ("deepseek", frozenset({"deepseek"}), ("deepseek",), ("api.deepseek.com",)),
    ("mimo", frozenset({"xiaomi"}), ("mimo",), ("api.xiaomimimo.com", "xiaomimimo.com")),
)


def _host_matches(base_url: Any, host: str) -> bool:
    try:
        parsed = urlparse(str(base_url or ""))
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname == host or hostname.endswith("." + host)


def reasoning_echo_family(provider: Any, model: Any, base_url: Any) -> str | None:
    """Which echo-back family this endpoint belongs to, if any.

    ``None`` means the strict side: ``reasoning_content`` must be stripped.
    """
    provider_lower = str(provider or "").lower()
    model_lower = str(model or "").lower()
    for family, providers, model_subs, hosts in _REASONING_ECHO_RULES:
        if provider_lower in providers:
            return family
        if any(sub in model_lower for sub in model_subs):
            return family
        if any(_host_matches(base_url, host) for host in hosts):
            return family
    return None


def needs_reasoning_echo(provider: Any, model: Any, base_url: Any) -> bool:
    return reasoning_echo_family(provider, model, base_url) is not None


# JSON-Schema keywords that a provider will try to *resolve* when they
# appear inside a tool result, rather than treating them as the data they
# are. Gemini via ai-studio rejects the whole request:
#
#   The referenced name `#/$defs/SearchContactsByCriteriaResponse` in
#   function_response.response does not match to a display_name in the
#   function_response.parts.
#
# Any tool that returns a schema hits this -- `composio_describe`,
# COMPOSIO_GET_TOOL_SCHEMAS, MCP tool listings. Observed live: one
# `composio_describe` of a HubSpot action poisoned every subsequent request
# in the Task with a permanent 400, and because the retry re-ran discovery
# from the top it reproduced the same 400 on every attempt.
_SCHEMA_REF_KEYS = ("$defs", "$ref", "$schema", "$id")


def neutralize_schema_refs(value: Any) -> Any:
    """Rename JSON-Schema reference keys inside tool output so a provider
    reads them as data instead of trying to resolve them.

    The model still sees the full structure and the same information -- only
    the sigil changes, so `$ref` becomes `ref_`. Deliberately done to the
    *content* rather than by dropping the keys: a tool schema without its
    definitions is actively misleading to a model about to call it.
    """
    if isinstance(value, dict):
        return {
            (f"{key[1:]}_" if key in _SCHEMA_REF_KEYS else key): neutralize_schema_refs(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [neutralize_schema_refs(item) for item in value]
    if isinstance(value, str) and "#/$" in value:
        # The pointer STRING is what the provider actually quotes back:
        #   The referenced name `#/$defs/SearchContactsByCriteriaResponse` ...
        # so renaming only the key left the payload just as poisonous.
        return value.replace("#/$", "#/")
    return value


def repair_empty_non_final_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Heal empty-content messages that are not the final turn.

    The final message is deliberately exempt: an empty trailing assistant
    turn is a prefill, which is the whole point of the continuation path.
    """
    if not messages:
        return messages
    repaired: list[dict[str, Any]] = []
    last = len(messages) - 1
    for index, message in enumerate(messages):
        if index == last:
            repaired.append(message)
            continue
        if _has_payload(message):
            repaired.append(message)
            continue
        repaired.append({**message, "content": INTERRUPTED_PLACEHOLDER})
    return repaired


def _has_payload(message: dict[str, Any]) -> bool:
    """Anything the API treats as non-empty content."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list) and content:
        return True
    if content not in (None, "") and not isinstance(content, (str, list)):
        return True
    return bool(message.get("tool_calls") or message.get("tool_call_id"))


def apply_reasoning_policy(
    messages: list[dict[str, Any]], *, echo_required: bool
) -> list[dict[str, Any]]:
    """Add or strip ``reasoning_content`` on assistant turns for this target.

    The requirement is narrow and the narrowness matters: these modes want
    reasoning_content on assistant **tool-call** messages, because replaying
    one without it answers HTTP 400. An ordinary assistant text turn is not
    covered, and padding one anyway describes a thinking turn whose thinking
    was blank -- deepseek-v4-flash answers that with a single token and
    finish_reason "stop", which reads at every layer above as "the model
    returned nothing" and fails the Task.

    So: reasoning the provider actually returned is always replayed; the
    single-space pad is only ever applied to a tool-call message, matching
    Hermes exactly. On the strict side every reasoning field is removed,
    since those providers reject the key outright.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            out.append(message)
            continue
        updated = dict(message)
        reasoning_text = updated.pop("reasoning", None)
        if not echo_required:
            updated.pop("reasoning_content", None)
            updated.pop("reasoning_details", None)
            out.append(updated)
            continue
        existing = updated.get("reasoning_content")
        if isinstance(existing, str) and existing:
            pass
        elif isinstance(reasoning_text, str) and reasoning_text.strip():
            updated["reasoning_content"] = reasoning_text
        elif updated.get("tool_calls"):
            # Pad only here. DeepSeek V4 Pro rejects the empty string, and a
            # space satisfies the non-empty check without fabricating
            # reasoning.
            updated["reasoning_content"] = " "
        out.append(updated)
    return out


def sanitize_api_messages(
    messages: list[dict[str, Any]], *, provider: Any, model: Any, base_url: Any
) -> list[dict[str, Any]]:
    """The one call every request goes through before the wire."""
    repaired = repair_empty_non_final_messages(list(messages))
    return apply_reasoning_policy(
        repaired, echo_required=needs_reasoning_echo(provider, model, base_url)
    )
