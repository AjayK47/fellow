"""Build the eval organization: scopes, people, rooms, and generated content.

The generated prose is written by a model and never committed. What *is*
committed is this generator plus `spec.py`, so anyone can rebuild an
equivalent corpus, and the labeled cases keep working because they refer to
beat keys rather than to generated ids.

Two paths, deliberately:

* ``--live`` asks the configured model to write Slack-shaped messages for
  each beat and a pile of unrelated filler. This is the corpus you measure
  retrieval on, because retrieval quality against templated text is a
  measurement of the template.
* without it, beats become their claim sentence verbatim and filler is
  synthesised locally. Useful for exercising the eval plumbing without
  spending anything -- the permission eval is fully valid on this corpus,
  the retrieval numbers are not.
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from toda.harness.retry import is_retryable_task_error

from evals import spec
from evals.spec import GeneratedCorpus

# A corpus build is hundreds of sequential provider calls; transient
# failures are expected rather than exceptional.
_MAX_INGEST_ATTEMPTS = 4

_FILLER_SYSTEM = """You write realistic internal Slack messages for a \
fictional logistics company. Output JSON only.

Rules:
- Ordinary working chatter: standups, questions, links, mild complaints,
  scheduling, half-finished thoughts. Not announcements.
- Vary length hard. Some messages are three words. A few are a paragraph.
- Never mention the topics you are told to avoid, even obliquely.
- No markdown headers, no bullet lists, no sign-offs. Slack, not email.
"""

_BEAT_SYSTEM = """You write realistic internal Slack messages for a \
fictional logistics company. Output JSON only.

You are given one fact that must be conveyed. Write a short exchange of 1-3
messages that communicates it the way colleagues actually would -- in
passing, with context assumed, not as a formal statement. Do not quote the
fact verbatim. The fact must be recoverable by a reader, but the phrasing
must be your own.
"""


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class CorpusBuilder:
    def __init__(self, brain: Any, *, live: bool, model: Any | None = None) -> None:
        self.brain = brain
        self.live = live
        self.model = model
        self.random = random.Random(20260815)

    # ---- organization scaffolding (never generated, always exact) ----

    def build_organization(self) -> None:
        store = self.brain.store
        store.put_organization(
            {
                "id": spec.ORG_ID,
                "name": spec.ORG_NAME,
                "region": "eu-west-1",
                "about": f"{spec.ORG_NAME} is a fictional company used only for evaluation.",
            }
        )
        for room in spec.ROOMS:
            store.upsert_scope(
                {
                    "id": room.scope_id,
                    "organization_id": spec.ORG_ID,
                    "kind": "organization_public" if room.kind == "public" else "private_channel",
                    "label": room.label,
                    "channel_id": room.id,
                    "is_public": room.kind == "public",
                    "principal_ids": list(room.members),
                }
            )
            for member in room.members:
                store.add_membership(spec.ORG_ID, member, room.scope_id)
        for person in spec.PEOPLE:
            self.brain.graph.upsert_entity(
                {
                    "id": person.id,
                    "organization_id": spec.ORG_ID,
                    "entity_type": "person",
                    "name": person.name,
                    "aliases": [person.name.split()[0]],
                    "attributes": {"role": person.role, "team": person.team},
                }
            )

    # ---- content ----

    def _generate(self, system: str, user: str, fallback: list[str]) -> list[str]:
        if not (self.live and self.model):
            return fallback
        for attempt in range(_MAX_INGEST_ATTEMPTS):
            try:
                response = self.model.generate(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.9,
                    max_tokens=800,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if not is_retryable_task_error(str(exc)) or attempt == _MAX_INGEST_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
        try:
            payload = json.loads(response.content)
            messages = [str(m).strip() for m in payload.get("messages") or [] if str(m).strip()]
            return messages or fallback
        except (json.JSONDecodeError, TypeError, ValueError):
            return fallback

    def _ingest(
        self,
        *,
        room: spec.Room,
        speaker: str,
        text: str,
        at: datetime,
        external_id: str,
        learn: bool = True,
    ) -> str:
        """Ingest one message, retrying transient provider failures.

        Production ingest is protected by the job queue, which retries a
        failed `knowledge.index_evidence` with backoff. This path calls the
        service directly, so it has no such net -- and a corpus build is
        hundreds of sequential provider calls over many minutes, which makes
        a single flaky embedding call near-certain. The first live build
        died on exactly that and threw away everything before it.
        """
        last: Exception | None = None
        for attempt in range(_MAX_INGEST_ATTEMPTS):
            try:
                result = self.brain.ingestion.ingest(
                    organization_id=spec.ORG_ID,
                    source_system="slack",
                    source_type="slack.channel_message",
                    external_id=external_id,
                    content=text,
                    scope_id=room.scope_id,
                    author_entity_id=speaker,
                    channel_id=room.id,
                    is_private_channel=room.kind == "private",
                    entity_ids=[],
                    metadata={"channel_id": room.id, "eval": True},
                    learn=learn,
                    processing_mode="memory" if learn else "reference",
                    occurred_at=at,
                )
                return result["source"]["id"]
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if not is_retryable_task_error(str(exc)) or attempt == _MAX_INGEST_ATTEMPTS - 1:
                    raise
                delay = 2**attempt
                print(
                    f"  transient ingest failure ({type(exc).__name__}), "
                    f"retrying in {delay}s [{attempt + 1}/{_MAX_INGEST_ATTEMPTS}]",
                    file=sys.stderr,
                )
                time.sleep(delay)
        raise last if last else RuntimeError("ingest failed")

    def build_beats(self, corpus: GeneratedCorpus) -> None:
        for beat in spec.BEATS:
            room = spec.room(beat.room)
            speaker = spec.person(beat.speaker)
            messages = self._generate(
                _BEAT_SYSTEM,
                json.dumps(
                    {
                        "company": spec.ORG_NAME,
                        "channel": room.label,
                        "speaker": f"{speaker.name} ({speaker.role})",
                        "fact_to_convey": beat.claim,
                        "output": {"messages": ["..."]},
                    }
                ),
                fallback=[beat.claim],
            )
            print(f"  beat: {beat.key}", file=sys.stderr)
            at = _iso(beat.at)
            source_id = ""
            for index, text in enumerate(messages):
                source_id = self._ingest(
                    room=room,
                    speaker=beat.speaker,
                    text=text,
                    at=at + timedelta(minutes=index),
                    external_id=f"eval:{beat.key}:{index}",
                )
            corpus.beat_sources[beat.key] = source_id

    def build_filler(self, corpus: GeneratedCorpus) -> None:
        avoid = [b.claim for b in spec.BEATS]
        for room in spec.ROOMS:
            speakers = list(room.members)
            fallback = [
                f"{spec.person(self.random.choice(speakers)).name.split()[0]}: "
                f"routine note {index} in {room.label}"
                for index in range(spec.FILLER_PER_ROOM)
            ]
            messages = self._generate(
                _FILLER_SYSTEM,
                json.dumps(
                    {
                        "company": spec.ORG_NAME,
                        "channel": room.label,
                        "participants": [spec.person(s).name for s in speakers],
                        "count": spec.FILLER_PER_ROOM,
                        "avoid_topics": avoid,
                        "output": {"messages": ["..."]},
                    }
                ),
                fallback=fallback,
            )
            print(f"  filler: {room.label} ({len(messages)} messages)", file=sys.stderr)
            base = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
            for index, text in enumerate(messages[: spec.FILLER_PER_ROOM]):
                corpus.filler_sources.append(
                    self._ingest(
                        room=room,
                        speaker=self.random.choice(speakers),
                        text=text,
                        at=base + timedelta(hours=index * 7),
                        external_id=f"eval:filler:{room.id}:{index}",
                        # Filler exists to be a distractor, not to be
                        # remembered. Extracting atomic memories from 160
                        # pieces of small talk is ~160 sequential model
                        # calls for content no case ever asserts on -- and
                        # in production most chatter does not become a
                        # memory either (harness/event_policy.py), so
                        # reference-only is the more faithful shape as well
                        # as the affordable one. It still chunks and embeds,
                        # so evidence search still has noise to sift.
                        learn=False,
                    )
                )

    def run(self) -> GeneratedCorpus:
        corpus = GeneratedCorpus(
            organization_id=spec.ORG_ID,
            generated_at=datetime.now(timezone.utc).isoformat(),
            generator_model=str(getattr(self.model, "model", "none")) if self.live else "none",
        )
        self.build_organization()
        self.build_filler(corpus)
        self.build_beats(corpus)
        # Memories are extracted by the ingestion pipeline; map them back to
        # their beat so retrieval cases can score either granularity.
        for key, source_id in corpus.beat_sources.items():
            corpus.beat_memories[key] = [
                memory["id"]
                for memory in self.brain.store.list_memories(spec.ORG_ID, latest_only=False)
                if source_id in (memory.get("source_ids") or [])
            ]
        return corpus


def write_manifest(corpus: GeneratedCorpus, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "organization_id": corpus.organization_id,
                "generated_at": corpus.generated_at,
                "generator_model": corpus.generator_model,
                "beat_sources": corpus.beat_sources,
                "beat_memories": corpus.beat_memories,
                "filler_count": len(corpus.filler_sources),
            },
            indent=2,
            sort_keys=True,
        )
    )
