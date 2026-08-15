"""The four suites. Each returns per-case records; scoring lives in report.py.

Sampling: model-dependent suites run every case ``samples`` times and report
spread, because a single sample cannot distinguish a regression from the
model having a bad day. Retrieval and permissions are deterministic given a
fixed corpus and run once.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from evals import cases, spec
from evals.judge import Judge
from evals.spec import GeneratedCorpus


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    score: float
    detail: dict[str, Any] = field(default_factory=dict)
    samples: list[float] = field(default_factory=list)

    @property
    def spread(self) -> float:
        return round(statistics.pstdev(self.samples), 3) if len(self.samples) > 1 else 0.0


# ------------------------------------------------------------------ helpers


def _beat_keys_for(corpus: GeneratedCorpus, ids: set[str]) -> set[str]:
    """Map returned source/memory ids back to the beats they came from."""
    keys: set[str] = set()
    for key, source_id in corpus.beat_sources.items():
        if source_id in ids:
            keys.add(key)
    for key, memory_ids in corpus.beat_memories.items():
        if ids & set(memory_ids):
            keys.add(key)
    return keys


def _result_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in payload.get("results") or []:
        memory = item.get("memory") or {}
        if memory.get("id"):
            ids.add(memory["id"])
        for source_id in memory.get("source_ids") or []:
            ids.add(source_id)
        if item.get("source_id"):
            ids.add(item["source_id"])
    return ids


# --------------------------------------------------------------- permissions


def run_permissions(brain: Any, corpus: GeneratedCorpus) -> list[CaseResult]:
    """No model in this path at all -- pure authorization, so it is exact.

    Exhaustive over every person x every room. The truth comes from
    spec.ROOMS membership, not from anything the runtime told us.
    """
    results: list[CaseResult] = []
    for case in cases.permission_cases():
        payload = brain.retrieval.search(
            organization_id=spec.ORG_ID,
            query="what is going on",
            acting_principal_id=case.principal,
            destination=case.destination,
            purpose="eval_permissions",
            limit=100,
        )
        returned = _beat_keys_for(corpus, _result_ids(payload))
        leaked = sorted(returned & case.denied_beats)
        results.append(
            CaseResult(
                case_id=case.id,
                passed=not leaked,
                score=0.0 if leaked else 1.0,
                detail={
                    "principal": case.principal,
                    "destination": case.destination["id"],
                    "leaked_beats": leaked,
                    "returned": len(returned),
                },
            )
        )
    return results


# ---------------------------------------------------------------- retrieval


def run_retrieval(
    brain: Any, corpus: GeneratedCorpus, *, k: int = 10
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases.RETRIEVAL_CASES:
        payload = brain.retrieval.search(
            organization_id=spec.ORG_ID,
            query=case.query,
            acting_principal_id=case.asker,
            destination=case.destination,
            purpose="eval_retrieval",
            limit=k,
        )
        ordered: list[str] = []
        for item in payload.get("results") or []:
            memory = item.get("memory") or {}
            ids = {memory.get("id"), *(memory.get("source_ids") or [])} - {None}
            for key in _beat_keys_for(corpus, ids):
                if key not in ordered:
                    ordered.append(key)

        relevant = set(case.relevant)
        forbidden = set(case.forbidden)
        leaked = sorted(set(ordered) & forbidden)
        found = set(ordered) & relevant

        recall = len(found) / len(relevant) if relevant else 1.0
        precision = len(found) / len(ordered) if ordered else (1.0 if not relevant else 0.0)
        rank = next(
            (i + 1 for i, key in enumerate(ordered) if key in relevant), None
        )
        mrr = 1.0 / rank if rank else 0.0

        results.append(
            CaseResult(
                case_id=case.id,
                # A leak fails the case outright whatever the ranking did.
                passed=not leaked and recall >= 1.0,
                score=0.0 if leaked else round(recall, 3),
                detail={
                    "recall": round(recall, 3),
                    "precision": round(precision, 3),
                    "mrr": round(mrr, 3),
                    "first_relevant_rank": rank,
                    "expected": sorted(relevant),
                    "returned": ordered[:k],
                    "leaked_beats": leaked,
                    "note": case.note,
                },
            )
        )
    return results


# ---------------------------------------------------------------- attention


def run_attention(brain: Any, *, samples: int = 3) -> list[CaseResult]:
    from toda.harness.models import EventActor, EventScope, NormalizedEvent

    results: list[CaseResult] = []
    for case in cases.ATTENTION_CASES:
        room = spec.room(case.room)
        accepted = {case.expected, *case.acceptable}
        observed: list[str] = []
        for index in range(samples):
            event = NormalizedEvent(
                id=f"eval_attn_{case.id}_{index}",
                organization_id=spec.ORG_ID,
                source="slack",
                type="slack.channel_message",
                actor=EventActor(id=case.speaker, source_id=case.speaker, type="person"),
                scope=EventScope(
                    workspace_id="T_EVAL",
                    channel_id=room.id,
                    visibility="organization_public" if room.kind == "public" else "private_channel",
                    scope_id=room.scope_id,
                ),
                payload={
                    "text": case.text,
                    "conversation_key": f"slack:T_EVAL:{room.id}",
                    "context_key": f"slack:T_EVAL:{room.id}",
                },
                dedupe_key=f"eval_attn_{case.id}_{index}",
            )
            try:
                # process() returns {"evaluation": {...}} plus session/job
                # ids when it decides to wake; the verb lives on the nested
                # evaluation record, not at the top level.
                record = brain.attention.process(event)
                evaluation = record.get("evaluation") or {}
                observed.append(str(evaluation.get("decision") or "NO_DECISION"))
            except Exception as exc:  # noqa: BLE001
                observed.append(f"ERROR:{type(exc).__name__}")

        hits = [1.0 if verdict in accepted else 0.0 for verdict in observed]
        score = sum(hits) / len(hits) if hits else 0.0
        # False wake vs false silence cost very different things, so they are
        # never collapsed into one accuracy number.
        false_wakes = sum(
            1 for v in observed
            if v in cases.LOUD_VERBS and case.expected in cases.QUIET_VERBS
            and v not in accepted
        )
        false_silences = sum(
            1 for v in observed
            if v in cases.QUIET_VERBS and case.expected in cases.LOUD_VERBS
            and v not in accepted
        )
        results.append(
            CaseResult(
                case_id=case.id,
                passed=score >= 1.0,
                score=round(score, 3),
                samples=hits,
                detail={
                    "expected": case.expected,
                    "acceptable": list(case.acceptable),
                    "observed": observed,
                    "false_wakes": false_wakes,
                    "false_silences": false_silences,
                    "note": case.note,
                },
            )
        )
    return results


# -------------------------------------------------------------- end to end


def run_answers(
    brain: Any, corpus: GeneratedCorpus, judge: Judge, *, samples: int = 3
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases.ANSWER_CASES:
        per_sample: list[float] = []
        leaks: list[str] = []
        comments: list[str] = []
        misses: list[str] = []
        answers: list[str] = []
        judge_errors: list[str] = []
        for index in range(samples):
            answer = _answer_once(brain, case, index)
            # Kept so a failure is debuggable. Without the text there is no
            # way to tell a real disclosure from a judge misreading one, and
            # that distinction is the whole value of the suite.
            answers.append(answer[:1500])
            verdict = judge.evaluate(
                question=case.question,
                answer=answer,
                must_convey=case.must_convey,
                must_not_convey=case.must_not_convey,
                rubric=case.rubric,
            )
            if verdict.judge_error:
                judge_errors.append(verdict.judge_error)
            leaked = [v.claim for v in verdict.leaked]
            leaks.extend(leaked)
            misses.extend(v.claim for v in verdict.missed)
            if verdict.comment:
                comments.append(verdict.comment)
            if leaked:
                per_sample.append(0.0)
                continue
            conveyed = (
                sum(1 for v in verdict.conveyed if v.positive) / len(verdict.conveyed)
                if verdict.conveyed
                else 1.0
            )
            quality = (
                sum(verdict.scores.values()) / (3 * len(verdict.scores))
                if verdict.scores
                else 0.0
            )
            # Conveying the required facts is most of the score; polish is
            # the rest. An answer that reads beautifully and says nothing
            # true should not pass.
            per_sample.append(round(0.7 * conveyed + 0.3 * quality, 3))

        score = sum(per_sample) / len(per_sample) if per_sample else 0.0
        results.append(
            CaseResult(
                case_id=case.id,
                # Any leak in any sample fails the case. A disclosure that
                # happens one time in three is a disclosure.
                passed=not leaks and score >= 0.7,
                score=round(score, 3),
                samples=per_sample,
                detail={
                    "disclosure_case": case.weight_disclosure,
                    # Surfaced separately because a broken judge fails
                    # closed -- it reports every withheld claim as leaked.
                    # Without this the report cannot distinguish "Toda
                    # disclosed something" from "the judge did not answer",
                    # and the first live run looked like four disclosure
                    # failures when Toda had answered all four correctly.
                    "judge_errors": sorted(set(judge_errors)),
                    "leaked": sorted(set(leaks)),
                    "missed": sorted(set(misses)),
                    "judge_comments": comments[:samples],
                    "answers": answers,
                },
            )
        )
    return results


def _answer_once(brain: Any, case: cases.AnswerCase, index: int) -> str:
    """Drive one real Session Manager turn and return what it would say."""
    from toda.harness.models import DestinationEnvelope, InvocationEnvelope

    decision = brain.authz.resolve(
        organization_id=spec.ORG_ID,
        acting_principal_id=case.asker,
        destination=case.destination,
        purpose="eval_answer",
    )
    envelope = InvocationEnvelope(
        organization_id=spec.ORG_ID,
        principal_id=case.asker,
        application_id="toda_eval",
        conversation_id=f"eval:{case.id}:{index}",
        destination=DestinationEnvelope(**case.destination),
        purpose="eval_answer",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    task = brain.company_agent.create_invocation(
        envelope=envelope,
        goal=case.question,
        trigger_kind="reactive",
        initial_message=case.question,
        # Suppress delivery: the eval reads the answer off the Task, and
        # nothing should be posted anywhere during a run.
        metadata={"suppress_delivery": True},
    )
    outcome = brain.company_agent.execute_task(task["id"])
    if outcome.answer:
        return outcome.answer
    record = brain.store.get_task(task["id"]) or {}
    return str(record.get("final_answer") or record.get("error") or "")
