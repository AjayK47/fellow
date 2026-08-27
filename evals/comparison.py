"""Repeatable model-candidate comparison over recorded logical runs."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * percentile))))
    return round(values[index], 3)


def summarize_trials(trials: Iterable[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        groups[(str(trial.get("candidate") or "unknown"), str(trial.get("scenario") or "unknown"), int(trial.get("scenario_version") or 1))].append(trial)
    summaries: list[dict[str, Any]] = []
    for (candidate, scenario, version), items in sorted(groups.items()):
        latencies = [float((item.get("metrics") or {}).get("wall_duration_ms")) for item in items if isinstance((item.get("metrics") or {}).get("wall_duration_ms"), (int, float))]
        costs = [float((item.get("metrics") or {}).get("cost", {}).get("amount")) for item in items if isinstance(((item.get("metrics") or {}).get("cost") or {}).get("amount"), (int, float))]
        tokens = [float(((item.get("metrics") or {}).get("tokens") or {}).get("total_tokens")) for item in items if isinstance(((item.get("metrics") or {}).get("tokens") or {}).get("total_tokens"), (int, float))]
        successful = sum(
            1
            for item in items
            if (
                bool(item["passed"])
                if "passed" in item
                else str(item.get("outcome")) in {"completed", "success"}
            )
        )
        privacy_failures = sum(1 for item in items if (item.get("privacy") or {}).get("violation") or item.get("unauthorized_mutation"))
        incomplete = sum(
            1
            for item in items
            if (item.get("completeness") or {}).get("status") not in {"complete", "pending"}
        )
        retry_counts = [
            float(((item.get("retries") or {}).get("count")))
            for item in items
            if isinstance((item.get("retries") or {}).get("count"), (int, float))
        ]
        quality_scores = [
            float(item["quality_score"])
            for item in items
            if isinstance(item.get("quality_score"), (int, float))
        ]
        human_scores = [
            float((item.get("human_score") or {})["score"])
            for item in items
            if isinstance((item.get("human_score") or {}).get("score"), (int, float))
        ]
        summaries.append({
            "candidate": candidate,
            "scenario": scenario,
            "scenario_version": version,
            "trials": len(items),
            "success_rate": round(successful / len(items), 3) if items else 0.0,
            "privacy_failures": privacy_failures,
            "incomplete_traces": incomplete,
            "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
            "tokens": {"mean": round(statistics.fmean(tokens), 3) if tokens else None},
            "cost_usd": {"mean": round(statistics.fmean(costs), 8) if costs else None, "known_trials": len(costs)},
            "retries": {"mean": round(statistics.fmean(retry_counts), 3) if retry_counts else None},
            "quality": {
                "judge_mean": round(statistics.fmean(quality_scores), 3) if quality_scores else None,
                "human_mean": round(statistics.fmean(human_scores), 3) if human_scores else None,
                "human_rated_trials": len(human_scores),
            },
            "failure_count": len(items) - successful,
            "correction_burden": round(
                statistics.fmean(
                    float((item.get("correction_burden") or 0)) for item in items
                ),
                3,
            ) if items else 0.0,
            "run_ids": [item.get("task_id") for item in items if item.get("task_id")],
        })
    fatal_trials = sum(
        1
        for items in groups.values()
        for item in items
        if (
            (item.get("privacy") or {}).get("violation")
            or item.get("unauthorized_mutation")
            or (item.get("completeness") or {}).get("status")
            not in {"complete", "pending"}
        )
    )
    return {
        "schema": 1,
        "summaries": summaries,
        "fatal_trials": fatal_trials,
        "comparison_is_valid": fatal_trials == 0,
    }


def run_trials(
    trial_factory: Callable[[str, Any, int], dict[str, Any]],
    *,
    candidates: Iterable[str],
    scenarios: Iterable[Any],
    samples: int = 3,
) -> dict[str, Any]:
    """Run each scenario version repeatedly through an existing app factory.

    ``trial_factory`` owns fixture reset and returns the API run-summary shape;
    this module only adds candidate/scenario identity and aggregates it.
    """
    if not 1 <= samples <= 5:
        raise ValueError("samples must be between 1 and 5")
    trials: list[dict[str, Any]] = []
    for candidate in candidates:
        for item in scenarios:
            for sample in range(samples):
                result = dict(trial_factory(candidate, item, sample))
                result.update({"candidate": candidate, "scenario": item.id, "scenario_version": item.version, "sample": sample + 1})
                trials.append(result)
    return {"trials": trials, "comparison": summarize_trials(trials)}


def load_trials(paths: Iterable[Path]) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("trials"), list):
            trials.extend(item for item in payload["trials"] if isinstance(item, dict))
        elif isinstance(payload, dict):
            trials.append(payload)
    return trials


def render_comparison(report: dict[str, Any]) -> str:
    lines = ["", "  Toda model comparison"]
    for item in report.get("comparison", {}).get("summaries", []):
        latency = item.get("latency_ms") or {}
        lines.append(
            f"  {item['candidate']:<18} {item['scenario']:<28} "
            f"success {item['success_rate']:.2f}  "
            f"p50/p95 {latency.get('p50') or '—'}/{latency.get('p95') or '—'} ms  "
            f"cost {((item.get('cost_usd') or {}).get('mean') or '—')}"
        )
    if report.get("comparison", {}).get("fatal_trials"):
        lines.append("  FATAL: privacy or trace-completeness failures invalidate the comparison.")
    lines.append("")
    return "\n".join(lines)
