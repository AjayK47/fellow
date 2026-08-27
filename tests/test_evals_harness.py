"""The deterministic tier of the eval suite, run as an ordinary test.

Only the permission suite is asserted here. Retrieval, attention and answer
quality all need a real model or real embeddings to mean anything, so they
belong to `toda-eval run`, not to CI -- a green retrieval score measured on
the deterministic hash embedder would be worse than no score, because it
would look like coverage.

What this does guarantee is that the harness itself keeps working: the
corpus builds, the cases resolve to real rows, the scorers run, and the
permission expectations still hold against the authorization code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import cases, corpus as corpus_mod, report as report_mod, spec, suites


@pytest.fixture
def eval_brain(tmp_path):
    """An eval organization built with providers pinned to deterministic.

    `settings=` rather than an environment variable or a monkeypatch: the
    embedding provider resolves from config at construction and is called on
    every ingest, so nothing weaker actually stops a real network call.
    """
    from toda.config import ModelsConfig, settings as loaded
    from toda.services.container import TodaApp

    pinned = loaded.model_copy(update={"models": ModelsConfig()})
    brain = TodaApp(db_path=tmp_path / "eval.db", settings=pinned)
    try:
        builder = corpus_mod.CorpusBuilder(brain, live=False, model=None)
        built = builder.run()
        brain.harness_worker.drain_until_idle(max_cycles=200)
        yield brain, built
    finally:
        brain.close()


def test_the_permission_suite_finds_no_disclosure(eval_brain):
    """Exhaustive: every person against every room.

    This is the one eval that is exact rather than scored -- there is no
    model anywhere in the path, so a failure here is a defect and never
    sampling noise.
    """
    brain, built = eval_brain
    results = suites.run_permissions(brain, built)
    assert len(results) == len(spec.PEOPLE) * len(spec.ROOMS)
    leaked = {
        result.case_id: result.detail["leaked_beats"]
        for result in results
        if not result.passed
    }
    assert not leaked, f"permission-scoped content crossed a boundary: {leaked}"


def test_private_room_content_is_reachable_by_its_own_members(eval_brain):
    """The counterpart the leak test cannot give you: a suite that denies
    everything would pass the test above and be worthless."""
    brain, built = eval_brain
    payload = brain.retrieval.search(
        organization_id=spec.ORG_ID,
        query="pricing headcount acquisition",
        acting_principal_id="p_mira",
        destination={"type": "private_channel", "id": "c_exec", "is_private": True},
        purpose="eval_selftest",
        limit=50,
    )
    reachable = suites._beat_keys_for(built, suites._result_ids(payload))
    assert reachable & {"exec.pricing.change", "exec.headcount", "exec.acquisition"}


def test_every_labeled_case_resolves_to_real_rows(eval_brain):
    """A case naming a beat that the corpus never produced would score as a
    silent zero forever."""
    _brain, built = eval_brain
    known = set(built.beat_sources)
    assert known == {beat.key for beat in spec.BEATS}
    for case in cases.RETRIEVAL_CASES:
        unknown = (set(case.relevant) | set(case.forbidden)) - known
        assert not unknown, f"{case.id} refers to unknown beats: {unknown}"
    for case in cases.ANSWER_CASES:
        unknown = set(case.must_cite_beats) - known
        assert not unknown, f"{case.id} refers to unknown beats: {unknown}"


def test_attention_cases_name_real_rooms_and_verbs(eval_brain):
    from toda.harness.models import AttentionDecision

    verbs = {item.value for item in AttentionDecision}
    rooms = {room.id for room in spec.ROOMS}
    people = {person.id for person in spec.PEOPLE}
    for case in cases.ATTENTION_CASES:
        assert case.room in rooms, case.id
        assert case.speaker in people, case.id
        assert case.expected in verbs, case.id
        assert set(case.acceptable) <= verbs, case.id


def test_a_regression_smaller_than_the_sampling_spread_is_called_noise():
    """The comparison exists to answer "did this change make Toda better",
    and the honest answer is often "cannot tell yet"."""
    baseline = {"suites": {"answers": {"mean_score": 0.80, "mean_spread": 0.10}}}
    within = {"suites": {"answers": {"mean_score": 0.74, "mean_spread": 0.10}}}
    beyond = {"suites": {"answers": {"mean_score": 0.55, "mean_spread": 0.05}}}
    assert "noise" in report_mod.compare(within, baseline)
    assert "WORSE" in report_mod.compare(beyond, baseline)


def test_a_judge_that_fails_reports_a_leak_rather_than_a_pass():
    """A drifting or broken judge must not be able to turn a disclosure into
    a passing score."""
    from evals.judge import Judge

    class Broken:
        model = "broken"

        def generate(self, **_kwargs):
            raise RuntimeError("judge unavailable")

    verdict = Judge(Broken()).evaluate(
        question="q",
        answer="an answer that leaks everything",
        must_convey=(),
        must_not_convey=("a secret",),
        rubric="",
    )
    assert verdict.judge_error
    assert [v.claim for v in verdict.leaked] == ["a secret"]


def test_scenario_comparison_and_candidate_contract():
    from evals.comparison import summarize_trials
    from evals.runner import _candidate_label

    comparison = summarize_trials([
        {
            "candidate": "candidate-leaky",
            "scenario": "question.simple",
            "scenario_version": 1,
            "outcome": "completed",
            "privacy": {"violation": True},
            "completeness": {"status": "complete"},
        }
    ])
    assert comparison["comparison_is_valid"] is False
    assert comparison["fatal_trials"] > 0
    assert _candidate_label("primary=openrouter-prod:openai/gpt-5") == (
        "primary", "openrouter-prod", "openai/gpt-5"
    )
    with pytest.raises(SystemExit, match="label=connection:model"):
        _candidate_label("deterministic")


def test_write_scenarios_require_explicit_operator_confirmation(tmp_path: Path):
    import argparse

    from evals.runner import scenarios_run
    from evals.scenarios import SCENARIOS

    args = argparse.Namespace(
        samples=1,
        mode="write",
        confirm_operator=False,
        candidate=["deterministic"],
        scenario=[SCENARIOS[0].id],
        category=None,
        output_dir=tmp_path,
        human_scores=None,
    )
    with pytest.raises(SystemExit, match="requires --confirm-operator"):
        scenarios_run(args)


def test_queue_fault_fixtures_create_and_verify_real_lease_conditions(eval_brain):
    from evals.faults import install
    from toda.domain.models import utcnow

    brain, _built = eval_brain
    now = utcnow().isoformat()
    brain.store.create_session(
        {
            "id": "eval-contention-session",
            "organization_id": spec.ORG_ID,
            "conversation_key": "eval:contention",
            "created_at": now,
            "updated_at": now,
        }
    )
    brain.store.bump_session_revision("eval-contention-session")
    task = {
        "id": "eval-fault-task",
        "organization_id": spec.ORG_ID,
        "session_id": "eval-contention-session",
    }

    contention = install(brain, "queue_contention")
    contention.before_execute(task)
    assert contention.verify()[0] is True

    restart = install(brain, "worker_restart")
    restart.before_execute(task)
    assert restart.verify()[0] is True
    jobs = brain.store.list_harness_jobs_for_task(spec.ORG_ID, task["id"])
    assert [(job["kind"], job["status"]) for job in jobs] == [
        ("eval.worker_restart", "completed")
    ]
