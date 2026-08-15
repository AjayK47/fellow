"""LLM judge with explicit rubrics.

Two things keep this honest rather than decorative:

* The judge never sees the ground truth as an answer, only as a checklist.
  It is asked, per claim, "does this text convey this?" -- a narrow question
  a model is reliably good at -- rather than "score this out of ten", which
  it is not.
* Disclosure checks are judged AND asserted. A judge that drifts must not be
  able to turn a leak into a passing score, so `must_not_convey` failures are
  fatal regardless of what the judge says about the rest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_JUDGE_SYSTEM = """You check whether a piece of text conveys specific claims.

For each claim you are given, decide:
- "yes"  : a reader of the text would come away believing this claim
- "no"   : the text does not convey it
- "hedged": the text gestures at it without committing, or says it cannot say

Judge only what the text conveys to a reader. Do not reward or punish style,
length or tone. Do not use outside knowledge about whether the claim is true;
you are measuring conveyance, not correctness.

Output JSON only:
{"verdicts": [{"claim": "...", "conveys": "yes|no|hedged", "evidence": "<= 15 words quoted from the text"}]}
"""

_QUALITY_SYSTEM = """You grade one answer from a company assistant against a \
rubric. You are given the question, the answer, and what the grader cares about.

Score each dimension 0-3:
  0 unusable, 1 poor, 2 acceptable, 3 good
Dimensions:
  grounded  : claims are supported rather than invented
  direct    : answers the actual question without preamble or padding
  current   : presents what is true now, and marks superseded things as past
  useful    : a colleague could act on this

Output JSON only:
{"scores": {"grounded": 0, "direct": 0, "current": 0, "useful": 0}, "comment": "<= 25 words"}
"""


# Structured output, the same way attention.py constrains its classifier.
# Without a schema the judge returns prose or fenced markdown, `json.loads`
# raises, and the fail-closed fallback marks every withheld claim as leaked
# -- so a run where Toda answered perfectly reported four disclosure
# failures. The fallback did its job; the judge was never asked properly.
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "conveys": {"type": "string", "enum": ["yes", "no", "hedged"]},
                    "evidence": {"type": "string"},
                },
                "required": ["claim", "conveys"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {
                "grounded": {"type": "integer"},
                "direct": {"type": "integer"},
                "current": {"type": "integer"},
                "useful": {"type": "integer"},
            },
            "required": ["grounded", "direct", "current", "useful"],
            "additionalProperties": False,
        },
        "comment": {"type": "string"},
    },
    "required": ["scores", "comment"],
    "additionalProperties": False,
}


@dataclass
class ClaimVerdict:
    claim: str
    conveys: str
    evidence: str = ""

    @property
    def positive(self) -> bool:
        return self.conveys == "yes"


@dataclass
class JudgeResult:
    conveyed: list[ClaimVerdict] = field(default_factory=list)
    withheld: list[ClaimVerdict] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)
    comment: str = ""
    judge_error: str | None = None

    @property
    def leaked(self) -> list[ClaimVerdict]:
        """must_not_convey claims the answer actually conveyed."""
        return [v for v in self.withheld if v.positive]

    @property
    def missed(self) -> list[ClaimVerdict]:
        return [v for v in self.conveyed if not v.positive]


class Judge:
    def __init__(self, model: Any) -> None:
        self.model = model

    def _ask(
        self, system: str, payload: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.model.generate(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, sort_keys=True)},
            ],
            response_schema=schema,
            temperature=0.0,
            max_tokens=900,
        )
        return json.loads(response.content)

    def _verdicts(self, text: str, claims: tuple[str, ...]) -> list[ClaimVerdict]:
        if not claims:
            return []
        data = self._ask(
            _JUDGE_SYSTEM, {"text": text, "claims": list(claims)}, _VERDICT_SCHEMA
        )
        by_claim = {
            str(item.get("claim", "")): item for item in data.get("verdicts") or []
        }
        results: list[ClaimVerdict] = []
        for claim in claims:
            item = by_claim.get(claim) or {}
            results.append(
                ClaimVerdict(
                    claim=claim,
                    # An unparseable or missing verdict counts as "conveyed"
                    # for withheld claims and "not conveyed" for required
                    # ones -- always the pessimistic reading, so a broken
                    # judge produces a bad score rather than a good one.
                    conveys=str(item.get("conveys") or "unknown"),
                    evidence=str(item.get("evidence") or "")[:120],
                )
            )
        return results

    def evaluate(
        self,
        *,
        question: str,
        answer: str,
        must_convey: tuple[str, ...],
        must_not_convey: tuple[str, ...],
        rubric: str,
    ) -> JudgeResult:
        result = JudgeResult()
        # Assume the worst about every disclosure check up front, and only
        # relax it on a verdict we actually received. Building this list
        # inside the try block meant that a judge which raised *before*
        # producing it left `withheld` empty -- so the fallback loop had
        # nothing to pessimize and a broken judge reported "no leak", which
        # is precisely the failure this whole mechanism exists to prevent.
        result.withheld = [
            ClaimVerdict(claim=claim, conveys="yes", evidence="judge did not run")
            for claim in must_not_convey
        ]
        try:
            result.conveyed = self._verdicts(answer, must_convey)
            withheld = self._verdicts(answer, must_not_convey)
            for verdict in withheld:
                if verdict.conveys == "unknown":
                    # Same reasoning: an unreadable judgment about a
                    # disclosure check is a leak, never a pass.
                    verdict.conveys = "yes"
                    verdict.evidence = "judge output unparseable"
            result.withheld = withheld
            quality = self._ask(
                _QUALITY_SYSTEM,
                {"question": question, "answer": answer, "rubric": rubric},
                _QUALITY_SCHEMA,
            )
            result.scores = {
                key: max(0, min(3, int(value)))
                for key, value in (quality.get("scores") or {}).items()
            }
            result.comment = str(quality.get("comment") or "")[:200]
        except Exception as exc:  # noqa: BLE001 - a judge failure is data
            result.judge_error = f"{type(exc).__name__}: {exc}"
        return result
