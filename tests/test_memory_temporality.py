"""A claim that stopped being true is not a claim to delete."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from toda.services.retrieval import _freshness

_NOW = datetime.now(timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def test_recency_breaks_ties_between_equally_relevant_claims():
    """Ranking had no notion of time: a 2024 claim and a 2026 claim about
    the same subject scored identically, so a stale answer won on a coin
    flip."""
    assert _freshness({"valid_from": _iso(0)}) > _freshness({"valid_from": _iso(180)})
    assert _freshness({"valid_from": _iso(180)}) > _freshness({"valid_from": _iso(360)})


def test_age_stops_counting_rather_than_going_negative():
    """Age is a tiebreaker, not a disqualifier -- an old claim about the
    right subject still beats a fresh one about the wrong subject."""
    assert _freshness({"valid_from": _iso(400)}) == 0.0
    assert _freshness({"valid_from": _iso(4000)}) == 0.0


def test_an_ended_claim_ranks_last_but_is_not_hidden():
    """"Ajay was on leave 1-10 August" is precisely what answers "was he
    around on the 3rd", so it stays retrievable -- it just must not outrank
    what is true now."""
    ended = {"valid_from": _iso(14), "valid_until": _iso(7)}
    current = {"valid_from": _iso(14)}
    assert _freshness(ended) == 0.0
    assert _freshness(current) > 0.0


def test_a_claim_with_no_dates_sits_in_the_middle():
    """No stated validity is the normal case and must not be penalised as
    though it were expired."""
    middle = _freshness({})
    assert 0.0 < middle < 1.0
    assert middle > _freshness({"valid_from": _iso(400)})


def test_supersession_closes_the_interval_when_the_new_claim_began(brain):
    """Told today that "we moved to ClickUp last March", the Linear claim
    stopped being true in March even though we only learned it now.
    Defaulting both timestamps to now makes "what were we using in April"
    unanswerable."""
    memories = brain.store.list_memories("org_acme", latest_only=True)
    assert memories
    old = memories[0]
    began = (_NOW - timedelta(days=120)).isoformat()

    brain.store.mark_superseded(old["id"], _NOW.isoformat(), valid_until=began)

    updated = brain.store.get_memory(old["id"])
    assert updated["valid_until"] == began, "world time is when the new claim began"
    assert updated["superseded_at"] == _NOW.isoformat(), "belief time is when we learned"
    assert updated["is_latest"] is False
    assert updated["status"] == "superseded"


def test_supersession_still_works_without_a_stated_start(brain):
    memories = brain.store.list_memories("org_acme", latest_only=True)
    old = memories[0]
    brain.store.mark_superseded(old["id"], _NOW.isoformat())
    updated = brain.store.get_memory(old["id"])
    assert updated["valid_until"] == _NOW.isoformat()
