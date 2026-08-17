"""Loads the prompt bodies in `prompts.md`, the single source of truth for
every model-facing instruction the harness sends.

A section is a `## <slug>` heading whose name is a lowercase slug; its body is
the first fenced block inside it. Headings that are not slugs (`## How to
edit`) are human documentation and are ignored, so the file stays readable as
a document while still being the thing the code actually loads.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_PATH = Path(__file__).with_name("prompts.md")

_SECTION_RE = re.compile(r"^## (?P<name>[a-z][a-z0-9_]*)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```[^\n]*\n(?P<body>.*?)^```\s*$", re.MULTILINE | re.DOTALL)


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    matches = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group("name")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        fence = _FENCE_RE.search(text, match.end(), end)
        if not fence:
            raise ValueError(
                f"prompts.md section '{name}' has no fenced prompt body. "
                "Use a non-slug heading for documentation-only sections."
            )
        if name in sections:
            raise ValueError(f"prompts.md defines '{name}' twice")
        sections[name] = fence.group("body").strip("\n")
    if not sections:
        raise ValueError("prompts.md defines no prompts")
    return sections


def load(name: str) -> str:
    """Return one prompt body verbatim."""
    try:
        return _sections()[name]
    except KeyError:
        available = ", ".join(sorted(_sections()))
        raise KeyError(f"unknown prompt {name!r}; prompts.md defines: {available}") from None


def render(name: str, **values: object) -> str:
    """Return a prompt body with its `{placeholder}`s substituted.

    Only for bodies written as templates -- a body containing literal braces
    (JSON examples) must be read with `load()`.
    """
    return load(name).format(**values)


def names() -> tuple[str, ...]:
    return tuple(sorted(_sections()))
