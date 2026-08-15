# Evals

`toda-eval` measures four things the test suite deliberately does not: whether
retrieval returns the right memories, whether permission-scoped content ever
crosses a boundary, whether the attention classifier stays quiet when it
should, and whether an answer is grounded, current and safe to send.

The tests prove the machinery works. These measure whether Toda is any good,
which is a different question and the one that decides whether a prompt
change was an improvement.

## What is committed, and what is not

Committed: `spec.py` (the fictional organization), `cases.py` (labeled
expectations), the scorers, the judge, the runner.

Not committed: `evals/data/` (the generated corpus and its SQLite database)
and `evals/results/` (every run's scores).

This works because **the spec pins the facts and the model only writes the
prose around them**. A case names `mem:acme.renewal.security_review`, never a
row id, so regenerating the corpus with different wording leaves every label
valid. A scorer that only works against one particular phrasing is measuring
memorization.

## Running it

```bash
# Generate the corpus. Needs a configured model; costs a few hundred calls.
uv run toda-eval build

# Everything, three samples per model-dependent case.
uv run toda-eval run

# Just the free, exact suite.
uv run toda-eval run --offline --suites permissions

# Record what "current" means, so later runs can be compared to it.
uv run toda-eval run --set-baseline
```

`--offline` pins deterministic providers by passing `Settings` into
`TodaApp`, not through the environment. That distinction matters: the
embedding provider resolves from config and is called on every ingest, so an
"offline" mode that only silenced the generator would still make real
billable calls — which is exactly what `benchmarks/organization_1000.py` did
for as long as it set `TODA_*` variables nothing reads.

On an offline corpus only the permission suite is meaningful. Retrieval
scores measured against the deterministic hash embedder tell you the
plumbing works and nothing about ranking, and the runner says so.

## Reading a result

```
  permissions  24/24       ██████████████████  mean 1.00
  retrieval     7/9        ██████████████····  mean 0.78  ±0.04
       ✗ renewal.current_owner        recall 0.0 of ['acme.renewal.owner_changed']

  vs baseline:
    retrieval    0.89 → 0.78 (-0.11, ±0.04)  ↓ WORSE
```

Three rules the report follows:

**A move smaller than the sampling spread is reported as noise.** Model
output varies; `--samples` exists so a single bad generation cannot look
like a regression. If the spread is large, raise it before believing a small
delta.

**A disclosure failure is not a score.** Any case where scoped content
reached somewhere it should not fails outright and is listed separately,
whatever the rest of the run looked like.

**The judge fails closed.** It is asked narrow "does this text convey this
claim" questions rather than "score this out of ten", and an unparseable or
erroring judgment on a `must_not_convey` claim counts as a leak. A drifting
judge must not be able to turn a disclosure into a passing grade.

## Adding a case

Add a `Beat` to `spec.py` if the world needs a new fact, then a case in
`cases.py` referring to it by key. Rebuild the corpus. `tests/test_evals_harness.py`
asserts that every case resolves to real rows, so a case naming a beat the
corpus never produced fails loudly instead of scoring a silent zero.

## What this does not cover yet

Tool-call trajectories, skill activation, memory extraction quality, entity
resolution, and injection resistance beyond what `tests/test_red_team.py`
already asserts. The four suites here were chosen because they are the ones
whose absence was actually costing something.
