---
name: watchers
display_name: General Watchers
description: Run a durable watch for meaningful external or company changes.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.watchers
activation: {mode: optional}
required_capabilities: {}
warnings:
  - Stay silent (or say briefly that nothing changed) on a run where nothing
    meaningful happened -- a schedule firing is not itself news.
  - Never report the same change twice; update the stored baseline before
    reporting, not after, so a delivery failure can't cause a repeat.
steps:
  - order: 1
    name: Baseline
    description: Record the last observed state in the shared workspace before comparing anything.
  - order: 2
    name: Detect
    description: Fetch or query current state and diff it against the stored baseline.
  - order: 3
    name: Notify
    description: Deliver a proportional, deduplicated update only when something actually changed.
---
This is Toda's general pattern for "watch X and tell me when it changes" --
a competitor's pricing page, a company metric, a repeated check on an
external system through a connected app, anything that isn't specifically a
blog (see the blogwatcher skill for that narrower case). A watch is not a
standing background process; it's a recurring `automation_create` goal that
re-runs the same check-compare-notify loop each time it fires.

## The building blocks

- **`automation_create`** schedules the recurring run (cron expression or
  interval). It pauses for human approval before it takes effect -- say so
  plainly rather than implying the watch is already live the moment you call
  it.
- **`read_file` / `write_file`** hold the durable baseline between
  runs. The team workspace is shared across the whole Slack workspace and
  persists across runs, so a value written by one automation firing is still
  there the next time it fires.
- **`web_fetch`**, **`memory_search`** / **`memory_query_facts`**, or a
  connected-app read through Composio are the actual "check" step, depending
  on what's being watched.
- **`artifact_create`** is worth using when the watch should also keep
  a durable log of what changed over time, not just a one-off Slack
  notification.

## Setting up a watch

1. Establish what "changed" concretely means for this watch -- a new row, a
   crossed threshold, a new value for a specific field -- not a vague "let
   me know if anything's different."
2. Pick a workspace path for the baseline, e.g. `watchers/<name>.json`, and
   write the current state there as the first baseline before ever reporting
   anything as new.
3. Call `automation_create` with a goal that names the exact thing to check
   and the exact comparison to make, e.g. "check the competitor pricing page
   at <url>; compare the listed price against the value stored in
   watchers/competitor-price.json; report only if it changed."
4. On each subsequent firing: read the baseline, fetch/query current state,
   diff the two, write the new baseline, and only then decide what (if
   anything) to report.

## Detecting real change vs. noise

Compare on the specific field or fact that matters, not the raw fetched
content wholesale -- a page's whitespace or unrelated section changing isn't
a meaningful change to a price watch. When the underlying source is noisy or
intermittently unavailable, treat a single failed fetch as inconclusive (say
so, don't report a false change) rather than comparing against stale or
empty data.

## Delivering proportionally

An automation-triggered run carries no inherent urgency just because it
fired -- see the system's own guidance on automation-triggered runs. If
nothing changed, either say so briefly or produce no output at all,
depending on what's proportionate for that channel. When something did
change, keep the notification to what changed and why it matters, not a
restatement of the entire monitored state.

## Multiple independent watches

Give each watch its own baseline path and its own automation, so one watch's
comparison never depends on another's state, and only watch what was
actually requested or previously configured -- don't expand scope to
"anything that seems interesting" on the same source.
