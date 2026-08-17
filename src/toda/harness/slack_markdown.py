"""Translate a model's Markdown into Slack's mrkdwn.

Slack does not speak the Markdown every model writes by default. It has no
headings, uses single asterisks for bold where Markdown uses two, single
underscores for italic, single tildes for strikethrough, and its own
``<url|label>`` link form. Posting GitHub-flavoured Markdown into a Slack
message renders it verbatim: ``### 1. What is Terafab?`` stays a literal
hash-prefixed line and ``**Terafab**`` keeps its asterisks.

Delivery has always declared ``{"type": "mrkdwn"}`` on its blocks while
sending the model's raw text, so the declaration was never true. This is
the missing half.

Fenced code and inline code are passed through untouched: an asterisk
inside a shell command or a diff is content, not formatting, and rewriting
it corrupts the thing the user asked for.
"""

from __future__ import annotations

import re

# Split on fenced blocks, keeping them, so conversion can skip their bodies.
_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)
# Inline code, same reasoning at a smaller scale.
_INLINE_CODE = re.compile(r"(`[^`\n]+`)")

_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", re.DOTALL)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_ITALIC_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9_])__(?=\S)(.+?)(?<=\S)__(?![A-Za-z0-9_])", re.DOTALL)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]]*)\]\(\s*<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_HRULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^(\s*)>\s?", re.MULTILINE)
_TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]*\|[\s:|-]*\|?\s*$", re.MULTILINE)


def _convert_segment(text: str) -> str:
    # Headings first: Slack has none, so they become a bold line. Done before
    # bold conversion so the emitted asterisks are not reprocessed.
    text = _HEADING.sub(lambda m: f"*{m.group(2).strip()}*" if m.group(2).strip() else "", text)
    text = _TABLE_DIVIDER.sub("", text)
    text = _HRULE.sub("─" * 20, text)
    text = _LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>" if m.group(1) else f"<{m.group(2)}>", text)
    # Bold+italic before bold, and bold before italic, so the longer marker
    # wins and its output is not re-matched by the shorter rule.
    text = _BOLD_ITALIC.sub(r"*_\1_*", text)
    text = _BOLD.sub(r"*\1*", text)
    text = _ITALIC_UNDERSCORE.sub(r"_\1_", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BLOCKQUOTE.sub(r"\1> ", text)
    return text


def to_slack_mrkdwn(text: str) -> str:
    """Rewrite Markdown as Slack mrkdwn, leaving code untouched."""
    if not text:
        return text
    out: list[str] = []
    for block in _FENCE.split(text):
        if block.startswith("```") or block.startswith("~~~"):
            # Slack only understands triple backticks.
            out.append("```" + block[3:-3] + "```" if block.startswith("~~~") else block)
            continue
        pieces: list[str] = []
        for piece in _INLINE_CODE.split(block):
            pieces.append(piece if piece.startswith("`") else _convert_segment(piece))
        out.append("".join(pieces))
    # Heading removal can leave a run of blank lines behind.
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip("\n")
