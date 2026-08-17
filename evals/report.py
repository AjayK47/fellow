"""Scoring and output. JSON for comparison, console for reading."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.suites import CaseResult

SCHEMA = 1


def summarize(name: str, results: list[CaseResult]) -> dict[str, Any]:
    scores = [r.score for r in results]
    spreads = [r.spread for r in results if r.samples]
    return {
        "suite": name,
        "cases": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "mean_score": round(statistics.fmean(scores), 3) if scores else 0.0,
        "min_score": round(min(scores), 3) if scores else 0.0,
        # Spread across samples of the same case. High spread means the
        # number above is not a measurement yet -- raise --samples before
        # believing a small change in it.
        "mean_spread": round(statistics.fmean(spreads), 3) if spreads else 0.0,
        "results": [
            {
                "case": r.case_id,
                "passed": r.passed,
                "score": r.score,
                "spread": r.spread,
                **r.detail,
            }
            for r in results
        ],
    }


def build_report(
    suites: dict[str, list[CaseResult]], *, meta: dict[str, Any]
) -> dict[str, Any]:
    summaries = {name: summarize(name, results) for name, results in suites.items()}
    return {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **meta,
        "totals": {
            "cases": sum(s["cases"] for s in summaries.values()),
            "passed": sum(s["passed"] for s in summaries.values()),
            "failed": sum(s["failed"] for s in summaries.values()),
        },
        "suites": summaries,
    }


def write_report(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return path


def _bar(score: float, width: int = 18) -> str:
    filled = int(round(score * width))
    return "█" * filled + "·" * (width - filled)


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    totals = report["totals"]
    lines.append("")
    lines.append(f"  Toda evals — {report['created_at'][:19]}Z")
    lines.append(f"  corpus: {report.get('corpus_mode')}   model: {report.get('model')}   samples: {report.get('samples')}")
    lines.append("")
    for name, suite in report["suites"].items():
        head = f"  {name:<12} {suite['passed']}/{suite['cases']}"
        lines.append(f"{head:<26} {_bar(suite['mean_score'])}  mean {suite['mean_score']:.2f}"
                     + (f"  ±{suite['mean_spread']:.2f}" if suite["mean_spread"] else ""))
        for item in suite["results"]:
            if item["passed"]:
                continue
            reason = ""
            if item.get("judge_errors"):
                reason = f"JUDGE FAILED {item['judge_errors'][0][:60]} (score not meaningful)"
            elif item.get("leaked_beats") or item.get("leaked"):
                reason = f"LEAK {item.get('leaked_beats') or item.get('leaked')}"
            elif item.get("missed"):
                reason = f"missed {item['missed']}"
            elif item.get("observed"):
                reason = f"expected {item.get('expected')}, got {item['observed']}"
            elif item.get("expected") is not None:
                reason = f"recall {item.get('recall')} of {item.get('expected')}"
            lines.append(f"       ✗ {item['case']:<28} {reason}"[:150])
        lines.append("")
    lines.append(f"  total {totals['passed']}/{totals['cases']} passed")
    # Any disclosure failure anywhere is called out separately: it is not a
    # score, it is a defect.
    judge_broken = [
        item["case"]
        for suite in report["suites"].values()
        for item in suite["results"]
        if item.get("judge_errors")
    ]
    # A leak the judge never actually assessed is not a leak, it is a broken
    # judge. Reporting them together would train the reader to ignore both.
    leaks = [
        item["case"]
        for suite in report["suites"].values()
        for item in suite["results"]
        if (item.get("leaked_beats") or item.get("leaked"))
        and not item.get("judge_errors")
    ]
    if judge_broken:
        lines.append(
            f"  JUDGE UNAVAILABLE for {len(judge_broken)} case(s): "
            f"{', '.join(judge_broken)} — their scores mean nothing."
        )
    if leaks:
        lines.append(f"  DISCLOSURE FAILURES: {', '.join(leaks)}")
    lines.append("")
    return "\n".join(lines)


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> str:
    """Did this change make Toda better or worse? The whole point."""
    lines = ["", "  vs baseline:"]
    for name, suite in current["suites"].items():
        before = (baseline.get("suites") or {}).get(name)
        if not before:
            lines.append(f"    {name:<12} (new)")
            continue
        delta = suite["mean_score"] - before["mean_score"]
        spread = max(suite["mean_spread"], before["mean_spread"])
        # A move smaller than the sampling spread is not a result.
        verdict = "→ noise" if abs(delta) <= spread else ("↑ better" if delta > 0 else "↓ WORSE")
        lines.append(
            f"    {name:<12} {before['mean_score']:.2f} → {suite['mean_score']:.2f} "
            f"({delta:+.2f}, ±{spread:.2f})  {verdict}"
        )
    lines.append("")
    return "\n".join(lines)
