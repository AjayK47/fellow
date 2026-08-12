# Toda organizational benchmark

`benchmarks/organization_1000.py` is the public, reproducible baseline for a
1,000-member installation. It creates one organization, 100 teams, 1,000
permission-scoped memories, a relational relationship graph, 2,000 durable root
tasks, and 250 queued jobs in a temporary SQLite database.

Run it from the repository root:

```bash
uv run --extra dev python benchmarks/organization_1000.py \
  --output benchmarks/results/local.json
```

The report includes seed throughput and repeated latency measurements for exact
alias resolution, graph neighbors, authorized memory lookup, task-history FTS,
concurrent searches, and atomic queue leases. It also asserts that a person
outside a team cannot retrieve that team's memory and that two leases never
claim the same job.

Performance numbers are evidence, not a universal promise: CPU, filesystem,
Python, and SQLite versions are recorded in every report. CI runs a reduced
correctness smoke on pull requests. The complete 1,000-member report runs on
`main` and by manual dispatch, and is retained as a workflow artifact.

We intentionally do not fail a release against an invented millisecond target.
Once enough comparable CI reports exist, regression thresholds should be based
on that history and reviewed when the runner class or dataset changes.
