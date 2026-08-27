"""Load prompt assets from package data.

Prompt prose lives in one plain-text file per concern.  Keeping the assets
out of Python makes review easier while avoiding a custom Markdown parser.
Composition and message ordering remain in ``harness.prompts``.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    directory = files("toda.harness.prompting").joinpath("templates")
    result: dict[str, str] = {}
    for resource in directory.iterdir():
        if resource.name.endswith(".txt"):
            result[resource.name.removesuffix(".txt")] = resource.read_text(encoding="utf-8").strip("\n")
    if not result:
        raise RuntimeError("no prompt assets were packaged")
    return result


def load(name: str) -> str:
    try:
        return _sections()[name]
    except KeyError:
        raise KeyError(f"unknown prompt {name!r}; available: {', '.join(names())}") from None


def render(name: str, **values: object) -> str:
    return load(name).format(**values)


def names() -> tuple[str, ...]:
    return tuple(sorted(_sections()))
