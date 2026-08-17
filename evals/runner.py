"""`toda eval` — build the corpus, run the suites, score, compare.

Provider selection is explicit here rather than environmental. Determinism
in this codebase is chosen by whether ``models.assignments`` resolves, so a
run either has a real model configured or it does not, and the report says
which. That matters: retrieval scores measured on the deterministic hash
embedder are meaningless, and a report that does not distinguish the two is
worse than no report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evals import corpus as corpus_mod
from evals import report as report_mod
from evals import spec, suites
from evals.judge import Judge

EVAL_ROOT = Path(__file__).resolve().parent
DATA_DIR = EVAL_ROOT / "data"
RESULTS_DIR = EVAL_ROOT / "results"

ALL_SUITES = ("permissions", "retrieval", "attention", "answers")
# Suites that need a real model to mean anything.
MODEL_SUITES = frozenset({"attention", "answers"})


def _has_real_model(brain: Any) -> bool:
    return getattr(brain.main_model, "model", "") != "deterministic-harness-v1"


def _has_real_embeddings(brain: Any) -> bool:
    return getattr(brain.embeddings, "model", "") != "deterministic-hash-v1"


def _settings_for(offline: bool) -> Any:
    """Pin which providers this run uses.

    `--offline` has to clear the model assignments, not just skip the
    generator: the embedding provider resolves from the same config and is
    called on every ingest, so an "offline" run that only silenced the
    writer still made real embedding calls. That is precisely the bug in
    benchmarks/organization_1000.py, which sets TODA_MODEL_PROVIDER and
    TODA_EMBEDDING_PROVIDER -- variables nothing has read since config.json
    became the single source of configuration.
    """
    from toda.config import ModelsConfig, settings as loaded

    if not offline:
        return None
    return loaded.model_copy(update={"models": ModelsConfig()})


def _open(offline: bool, db_path: Path) -> Any:
    from toda.services.container import TodaApp

    return TodaApp(db_path=db_path, settings=_settings_for(offline))


def build(args: argparse.Namespace) -> int:
    db_path = DATA_DIR / "eval.db"
    if db_path.exists():
        db_path.unlink()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    brain = _open(args.offline, db_path)
    try:
        live = not args.offline
        if live and not _has_real_model(brain):
            print(
                "No model is configured, so the corpus would be templated text.\n"
                "Configure models.assignments in config.json, or pass --offline "
                "to build a plumbing-only corpus.",
                file=sys.stderr,
            )
            return 2
        builder = corpus_mod.CorpusBuilder(
            brain, live=live, model=brain.main_model if live else None
        )
        print(f"Building corpus ({'live' if live else 'offline'})…", file=sys.stderr)
        built = builder.run()
        # Embeddings and extraction run on the knowledge lane.
        brain.harness_worker.drain_until_idle(max_cycles=200)
        corpus_mod.write_manifest(built, DATA_DIR / "corpus.json")
        print(
            f"  {len(built.beat_sources)} beats, {len(built.filler_sources)} filler, "
            f"{sum(len(v) for v in built.beat_memories.values())} memories",
            file=sys.stderr,
        )
        return 0
    finally:
        brain.close()


def _load_corpus() -> spec.GeneratedCorpus:
    manifest = DATA_DIR / "corpus.json"
    if not manifest.is_file():
        raise SystemExit("No corpus. Run: toda eval build")
    data = json.loads(manifest.read_text())
    return spec.GeneratedCorpus(
        organization_id=data["organization_id"],
        beat_sources=data["beat_sources"],
        beat_memories=data["beat_memories"],
        generated_at=data.get("generated_at", ""),
        generator_model=data.get("generator_model", ""),
    )


def run(args: argparse.Namespace) -> int:
    db_path = DATA_DIR / "eval.db"
    if not db_path.is_file():
        raise SystemExit("No corpus database. Run: toda eval build")
    built = _load_corpus()
    brain = _open(args.offline, db_path)
    try:
        selected = [s for s in ALL_SUITES if s in (args.suites or ALL_SUITES)]
        needs_model = bool(set(selected) & MODEL_SUITES)
        if needs_model and not _has_real_model(brain):
            print(
                f"Suites {sorted(set(selected) & MODEL_SUITES)} need a configured model. "
                "Run with --suites permissions retrieval, or configure one.",
                file=sys.stderr,
            )
            return 2
        if "retrieval" in selected and not _has_real_embeddings(brain):
            print(
                "WARNING: deterministic embeddings are in use, so retrieval "
                "scores measure plumbing, not ranking quality.",
                file=sys.stderr,
            )

        results: dict[str, list[suites.CaseResult]] = {}
        if "permissions" in selected:
            print("permissions…", file=sys.stderr)
            results["permissions"] = suites.run_permissions(brain, built)
        if "retrieval" in selected:
            print("retrieval…", file=sys.stderr)
            results["retrieval"] = suites.run_retrieval(brain, built, k=args.k)
        if "attention" in selected:
            print(f"attention ({args.samples} samples)…", file=sys.stderr)
            results["attention"] = suites.run_attention(brain, samples=args.samples)
        if "answers" in selected:
            print(f"answers ({args.samples} samples)…", file=sys.stderr)
            judge = Judge(brain.tool_output_summary_model)
            results["answers"] = suites.run_answers(
                brain, built, judge, samples=args.samples
            )

        report = report_mod.build_report(
            results,
            meta={
                "corpus_mode": "live" if built.generator_model != "none" else "offline",
                "corpus_generated_at": built.generated_at,
                "model": getattr(brain.main_model, "model", "deterministic"),
                "embeddings": getattr(brain.embeddings, "model", "deterministic"),
                "samples": args.samples,
                "k": args.k,
            },
        )
        print(report_mod.render(report))

        baseline_path = RESULTS_DIR / "baseline.json"
        if baseline_path.is_file() and not args.set_baseline:
            print(report_mod.compare(report, json.loads(baseline_path.read_text())))

        stamp = report["created_at"].replace(":", "").replace("-", "")[:15]
        written = report_mod.write_report(report, RESULTS_DIR / f"run-{stamp}.json")
        print(f"  written to {written}", file=sys.stderr)
        if args.set_baseline:
            report_mod.write_report(report, baseline_path)
            print(f"  baseline updated: {baseline_path}", file=sys.stderr)

        failed = report["totals"]["failed"]
        return 1 if (failed and args.strict) else 0
    finally:
        brain.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="toda eval",
        description="Measure retrieval, permissions, attention and answer quality.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="generate the eval corpus (not committed)")
    build_cmd.add_argument(
        "--offline",
        action="store_true",
        help="template the corpus instead of generating it; permissions stay valid, retrieval does not",
    )
    build_cmd.set_defaults(func=build)

    run_cmd = sub.add_parser("run", help="run the suites against the built corpus")
    run_cmd.add_argument("--suites", nargs="*", choices=ALL_SUITES, default=None)
    run_cmd.add_argument(
        "--samples", type=int, default=3,
        help="model-dependent cases are run this many times and the spread reported",
    )
    run_cmd.add_argument("--k", type=int, default=10, help="retrieval cutoff")
    run_cmd.add_argument(
        "--offline", action="store_true",
        help="pin deterministic providers; only the permission suite stays meaningful",
    )
    run_cmd.add_argument("--set-baseline", action="store_true")
    run_cmd.add_argument(
        "--strict", action="store_true", help="exit non-zero when any case fails"
    )
    run_cmd.set_defaults(func=run)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
