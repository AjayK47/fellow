"""Deterministic/heuristic memory extractor (no LLM required)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from toda.domain.models import (
    CanonicalClaim,
    Memory,
    MemoryType,
    new_id,
    utcnow,
)


@dataclass
class ExtractionResult:
    memories: list[Memory]
    entity_mentions: list[str]


# Patterns that produce typed memories
_DECISION = re.compile(
    r"(?i)\b(?:decided|decision|we (?:will|shall)|selected|chose|approved)\b(.{10,200})"
)
_COMMITMENT = re.compile(
    r"(?i)\b(?:will deliver|committed to|promise(?:s|d)?|owe(?:s)?|due by|by \d{4}-\d{2}-\d{2})\b(.{5,200})"
)
_ISSUE = re.compile(
    r"(?i)\b(?:issue|incident|outage|failure|stuck|broken|error|down|blocked)\b(.{5,200})"
)
_FACT_IS = re.compile(
    r"(?i)\b([A-Za-z0-9_\- ]{2,40})\s+(?:is|are|was|were|uses|owns|supports|has)\s+([^\.\n]{3,120})"
)
_LAUNCH = re.compile(
    r"(?i)\b(?:launch date|release date|ship date)\s+(?:is|was|moved to|changed to)?\s*([A-Za-z0-9 ,\-]+)"
)
_RELATIONSHIP = re.compile(
    r"(?i)\b([A-Za-z][A-Za-z0-9_\- ]+)\s+(?:works with|collaborates with|reports to|owns account for)\s+([A-Za-z][A-Za-z0-9_\- ]+)"
)


class DeterministicExtractor:
    """Pluggable extractor interface implementation."""

    name = "deterministic_v1"
    model_version = "heuristic-1"

    def extract(
        self,
        *,
        organization_id: str,
        content: str,
        scope_id: str,
        source_id: str,
        entity_ids: list[str] | None = None,
        participant_ids: list[str] | None = None,
        author_entity_id: str | None = None,
        is_private: bool = False,
        entity_name_map: dict[str, str] | None = None,
        occurred_at: Any | None = None,
        observed_at: Any | None = None,
    ) -> ExtractionResult:
        entity_ids = list(entity_ids or [])
        participant_ids = list(participant_ids or [])
        entity_name_map = entity_name_map or {}
        memories: list[Memory] = []
        mentions: list[str] = list(entity_ids)

        # Resolve named entities mentioned in text
        lower = content.lower()
        for eid, name in entity_name_map.items():
            if name.lower() in lower and eid not in mentions:
                mentions.append(eid)

        def _span(match: re.Match[str]) -> dict[str, Any]:
            return {
                "source_id": source_id,
                "start": match.start(),
                "end": match.end(),
                "text": match.group(0)[:500],
            }

        def _base(**kwargs: Any) -> Memory:
            eids = kwargs.pop("entity_ids", None)
            if eids is None:
                eids = list(dict.fromkeys(mentions))
            else:
                eids = list(dict.fromkeys(list(eids) + mentions))
            return Memory(
                id=new_id("mem"),
                organization_id=organization_id,
                scope_id=scope_id,
                source_ids=[source_id],
                entity_ids=eids,
                author_entity_id=author_entity_id,
                participant_ids=participant_ids,
                is_private=is_private,
                extraction_method=self.name,
                model_version=self.model_version,
                valid_from=occurred_at or utcnow(),
                observed_at=observed_at or utcnow(),
                created_at=utcnow(),
                **kwargs,
            )

        for m in _DECISION.finditer(content):
            claim = m.group(0).strip()
            memories.append(
                _base(
                    memory_type=MemoryType.DECISION,
                    content=claim,
                    confidence=0.9,
                    authority=0.85,
                    source_spans=[_span(m)],
                    canonical_claim=CanonicalClaim(
                        subject=mentions[0] if mentions else "unknown",
                        predicate="decision",
                        object=claim[:80],
                    ),
                )
            )

        for m in _COMMITMENT.finditer(content):
            claim = m.group(0).strip()
            memories.append(
                _base(
                    memory_type=MemoryType.COMMITMENT,
                    content=claim,
                    confidence=0.88,
                    authority=0.8,
                    source_spans=[_span(m)],
                )
            )

        for m in _ISSUE.finditer(content):
            claim = m.group(0).strip()
            memories.append(
                _base(
                    memory_type=MemoryType.ISSUE,
                    content=claim,
                    confidence=0.9,
                    authority=0.85,
                    source_spans=[_span(m)],
                )
            )

        for m in _LAUNCH.finditer(content):
            date_val = m.group(1).strip()
            claim = f"The launch date is {date_val}."
            subject = mentions[0] if mentions else "project"
            memories.append(
                _base(
                    memory_type=MemoryType.FACT,
                    content=claim,
                    confidence=0.95,
                    authority=0.9,
                    source_spans=[_span(m)],
                    canonical_claim=CanonicalClaim(
                        subject=subject,
                        predicate="launch_date",
                        object=date_val,
                    ),
                )
            )

        for m in _RELATIONSHIP.finditer(content):
            a, b = m.group(1).strip(), m.group(2).strip()
            claim = f"{a} relationship with {b}: {m.group(0).strip()}"
            memories.append(
                _base(
                    memory_type=MemoryType.RELATIONSHIP,
                    content=claim,
                    confidence=0.85,
                    authority=0.75,
                    source_spans=[_span(m)],
                )
            )

        for m in _FACT_IS.finditer(content):
            subj, obj = m.group(1).strip(), m.group(2).strip()
            # skip if already captured as decision/issue-like
            if any(k in m.group(0).lower() for k in ("decided", "issue", "incident", "failure")):
                continue
            claim = f"{subj} is {obj}".strip()
            # map subject name to entity id if possible
            subject_id = mentions[0] if mentions else subj
            for eid, name in entity_name_map.items():
                if name.lower() == subj.lower() or subj.lower() in name.lower():
                    subject_id = eid
                    if eid not in mentions:
                        mentions.append(eid)
                    break
            memories.append(
                _base(
                    memory_type=MemoryType.FACT,
                    content=claim,
                    confidence=0.86,
                    authority=0.8,
                    source_spans=[_span(m)],
                    entity_ids=list(dict.fromkeys(mentions)),
                    canonical_claim=CanonicalClaim(
                        subject=subject_id,
                        predicate="is",
                        object=obj[:120],
                    ),
                )
            )

        # If nothing matched, still store a low-authority episode summary for searchability
        if not memories and content.strip():
            memories.append(
                _base(
                    memory_type=MemoryType.EPISODE_SUMMARY,
                    content=content.strip()[:500],
                    confidence=0.5,
                    authority=0.4,
                    source_spans=[
                        {
                            "source_id": source_id,
                            "start": 0,
                            "end": min(len(content), 500),
                            "text": content[:500],
                        }
                    ],
                    status="active",
                )
            )

        # ensure entity_ids updated on all
        for mem in memories:
            mem.entity_ids = list(dict.fromkeys(list(mem.entity_ids) + mentions))

        return ExtractionResult(memories=memories, entity_mentions=mentions)
