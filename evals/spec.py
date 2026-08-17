"""The fictional organization every eval runs against.

This file pins **facts**; a model writes the prose around them at generation
time. That split is what lets the corpus stay out of Git while the labels
stay in it: a retrieval case names `mem:acme.renewal.security_review`, not a
UUID that changes every time the corpus is regenerated. Regenerating gives
you different wording for the same world, which is exactly what you want
from an eval corpus — a scorer that only works against one particular phrasing
is measuring memorization.

Nothing here is real. The people, company and incidents are invented, and no
content from any actual workspace belongs in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ORG_ID = "org_eval"
ORG_NAME = "Northwind Logistics"


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    team: str


@dataclass(frozen=True)
class Room:
    """A channel or DM. `members` is authoritative for the permission eval."""

    id: str
    scope_id: str
    label: str
    kind: Literal["public", "private", "dm"]
    members: tuple[str, ...]


@dataclass(frozen=True)
class Beat:
    """One fact the corpus must contain, and where it must live.

    `key` is the stable symbolic id an eval case refers to. `claim` is what
    is true; the generator writes Slack-shaped prose that conveys it without
    quoting this string. `supersedes` marks a claim that replaces an earlier
    one, which is how the temporal cases get something to be wrong about.
    """

    key: str
    room: str
    speaker: str
    claim: str
    at: str
    supersedes: str | None = None
    entities: tuple[str, ...] = ()


PEOPLE: tuple[Person, ...] = (
    Person("p_dana", "Dana Okafor", "VP Revenue", "revenue"),
    Person("p_sam", "Sam Reyes", "Account Executive", "revenue"),
    Person("p_kit", "Kit Alvarez", "Staff Engineer", "platform"),
    Person("p_wren", "Wren Baptiste", "Engineering Manager", "platform"),
    Person("p_lior", "Lior Haddad", "Support Lead", "support"),
    Person("p_mira", "Mira Sandoval", "CFO", "exec"),
)

ROOMS: tuple[Room, ...] = (
    Room("c_general", "scope_org_public", "#general", "public",
         tuple(p.id for p in PEOPLE)),
    Room("c_platform", "scope_room_platform", "#platform", "public",
         ("p_kit", "p_wren", "p_lior")),
    Room("c_revenue", "scope_room_revenue", "#revenue", "public",
         ("p_dana", "p_sam", "p_mira")),
    # The room the permission eval exists for: leadership-only, and several
    # beats below are the only place their claims appear.
    Room("c_exec", "scope_room_exec", "#exec", "private",
         ("p_dana", "p_mira")),
)

# Storylines. Each beat becomes one or more generated messages; the claim is
# what an eval is allowed to assert about.
BEATS: tuple[Beat, ...] = (
    # --- Renewal storyline: the canonical "what is blocking X" question ---
    Beat("acme.renewal.opened", "c_revenue", "p_sam",
         "Northwind's largest customer, Calderon Freight, has a renewal due at the end of the quarter.",
         "2026-04-02T09:00:00+00:00", entities=("Calderon Freight",)),
    Beat("acme.renewal.security_review", "c_revenue", "p_sam",
         "The Calderon Freight renewal is blocked on a security review of the export API.",
         "2026-04-09T11:20:00+00:00", entities=("Calderon Freight",)),
    Beat("acme.renewal.owner_kit", "c_platform", "p_wren",
         "Kit Alvarez owns the export API security review.",
         "2026-04-10T08:15:00+00:00", entities=("Kit Alvarez",)),
    Beat("acme.renewal.owner_changed", "c_platform", "p_wren",
         "Wren Baptiste has taken over the export API security review from Kit, who is on leave.",
         "2026-05-04T08:15:00+00:00", supersedes="acme.renewal.owner_kit",
         entities=("Wren Baptiste", "Kit Alvarez")),
    Beat("acme.renewal.unblocked", "c_revenue", "p_dana",
         "The security review for Calderon Freight finished and the renewal is no longer blocked.",
         "2026-05-20T16:00:00+00:00", supersedes="acme.renewal.security_review",
         entities=("Calderon Freight",)),

    # --- Incident storyline: decisions that later get reversed ---
    Beat("export.incident.cause", "c_platform", "p_kit",
         "The export worker outage was caused by a connection pool exhausted by retry storms.",
         "2026-03-14T22:40:00+00:00"),
    Beat("export.decision.queue", "c_platform", "p_wren",
         "The team decided to put exports behind a durable queue rather than raising the pool size.",
         "2026-03-16T10:00:00+00:00"),
    Beat("export.decision.reversed", "c_platform", "p_wren",
         "The durable queue plan was dropped in favour of backpressure, because the queue added latency nobody wanted.",
         "2026-06-01T10:00:00+00:00", supersedes="export.decision.queue"),

    # --- Exec-only storyline: nothing here may reach a non-member ---
    Beat("exec.pricing.change", "c_exec", "p_mira",
         "Northwind will raise list pricing by twelve percent in the third quarter.",
         "2026-04-22T13:00:00+00:00"),
    Beat("exec.acquisition", "c_exec", "p_dana",
         "Northwind is in early talks to acquire a regional competitor called Halden Transit.",
         "2026-05-11T15:30:00+00:00", entities=("Halden Transit",)),
    Beat("exec.headcount", "c_exec", "p_mira",
         "Platform team headcount is frozen until the pricing change lands.",
         "2026-05-12T09:00:00+00:00"),

    # --- Support storyline: ordinary noise that should mostly be ignored ---
    Beat("support.hours", "c_general", "p_lior",
         "Support coverage now runs until 8pm UK time on weekdays.",
         "2026-02-03T09:00:00+00:00"),
    Beat("support.tooling", "c_general", "p_lior",
         "Support moved its macros into the shared help centre.",
         "2026-02-19T14:00:00+00:00"),
)


# How many filler messages to generate per room. Retrieval metrics are
# meaningless on a corpus where every document is a labeled answer -- the
# distractors are the point.
FILLER_PER_ROOM = 40


def person(pid: str) -> Person:
    return next(p for p in PEOPLE if p.id == pid)


def room(rid: str) -> Room:
    return next(r for r in ROOMS if r.id == rid)


def beat(key: str) -> Beat:
    return next(b for b in BEATS if b.key == key)


def rooms_for(principal_id: str) -> tuple[Room, ...]:
    return tuple(r for r in ROOMS if principal_id in r.members)


def superseded_keys() -> frozenset[str]:
    return frozenset(b.supersedes for b in BEATS if b.supersedes)


@dataclass
class GeneratedCorpus:
    """What generate.py writes to disk. Never committed."""

    organization_id: str
    beat_sources: dict[str, str] = field(default_factory=dict)
    """beat key -> source id, so cases resolve symbols to real rows."""
    beat_memories: dict[str, list[str]] = field(default_factory=dict)
    """beat key -> memory ids extracted from that source."""
    filler_sources: list[str] = field(default_factory=list)
    generated_at: str = ""
    generator_model: str = ""
