---
name: planning-execution
display_name: Planning and Execution
description: Turn a goal into bounded, verifiable work with explicit dependencies.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.planning_execution
activation: {mode: optional}
required_capabilities: {}
warnings:
  - A plan is a structured run record, not a markdown file -- keep it current with plan_update as reality changes rather than letting it silently drift stale.
  - Every step needs a stated completion condition. "Investigate X" is not checkable; "report the three most likely causes with evidence" is.
steps:
  - order: 1
    name: Define outcome
    description: State the done condition and constraints before breaking anything into steps.
  - order: 2
    name: Sequence
    description: Order steps by real dependency, size each so its completion is checkable, and record them with plan_create.
  - order: 3
    name: Execute and update
    description: Work in dependency order, and call plan_update whenever a step's status or the plan itself changes.
---
# Planning and Execution

Turn a goal into a small number of ordered, checkable steps before working
through them -- not as ceremony, but so dependencies are explicit, progress
is visible, and a step someone else needs to see (a child Task, a human
reviewing later) isn't only in your own head.

## Toda's plan is a structured object, not a document

Unlike a markdown file written to disk, Toda's plan is a first-class
object attached to the current run:

- `plan_create` -- create the plan once, with a `title` and an `items` array.
  Each item can carry an `id`, `title`, `status`
  (`pending` / `active` / `blocked` / `completed` / `cancelled`), an `owner`,
  a list of `dependencies` (other item ids that must complete first), an
  `evidence` list, and a `blocker` note. Duplicate ids and dependencies on
  unknown ids are both rejected -- the plan can't silently reference a step
  that doesn't exist.
- `plan_get` -- read the plan back as it currently stands.
- `plan_update` -- replace the title and/or items. There's no separate
  "mark this one step done" call; update the whole `items` array with the
  changed statuses (and any new `evidence`) each time something changes.

This means the plan is always inspectable mid-run -- by you, by a
child Task you delegate to, or by whoever is watching the Task -- rather than
a file that has to be found and re-read. Keep it that way: call
`plan_update` when reality changes, not just at the end.

## When to use

- The goal has more than one real, orderable step, or has dependencies
  between steps.
- The work will be delegated to `delegate_tasks`
  child Tasks) or checked in on later, and needs to be legible to something
  other than your own immediate reasoning.

## When a plan is overkill

A single, obviously atomic action doesn't need a `plan_create` call around
it -- just do it. Planning ceremony around a one-step task adds latency
without adding clarity.

## Procedure

### 1. Define the outcome

Before writing any steps, state:

- The done condition -- what observably true fact means this is finished.
- Constraints -- deadlines, scopes, things explicitly out of bounds.
- Assumptions you're making, so they're visible if one turns out wrong.

### 2. Sequence into checkable steps

Break the goal into steps ordered by real dependency, not just narrative
order. For each step, before calling `plan_create`:

- Give it a title specific enough that "done" is unambiguous -- "identify
  the three most likely causes with supporting evidence" rather than
  "investigate the issue."
- Set `dependencies` to the ids of steps that must complete first. Steps with
  no dependency on each other can run in parallel later (via `delegate_tasks`)
  if that's worth the overhead.
- Leave status as `pending` for anything not yet started.

Call `plan_create` once with the full initial item list. Re-reading it later
via `plan_get` should be enough for anyone (including a delegated
child Task) to understand where things stand without asking you.

### 3. Execute in dependency order, and keep the plan current

Work through steps respecting their dependencies. As you go:

- Move a step to `active` when you start it, `completed` when its done
  condition is actually met (with `evidence` describing what proves it), or
  `blocked` with a `blocker` note if something outside your control is in
  the way.
- Call `plan_update` with the revised `items` array whenever status changes
  — don't batch every update until the very end, since the value of a live
  plan is that it reflects live state.
- If evidence gathered mid-execution changes what the remaining steps should
  be (a step turns out unnecessary, a new one is needed), update the plan to
  match reality rather than quietly deviating from a stale plan.

## Delegation

For independent steps with no shared dependency, `delegate_tasks` can run up to
three child Tasks in parallel, each
with its own prompt, tool grant, and token/time budget. This is a good
fit once a step is genuinely self-contained -- give the child Task the
step's done condition in its prompt so its report tells you
directly whether the step can move to `completed`.

## Pitfalls

- **Vague step titles.** "Look into X" has no checkable completion
  condition; "produce a list of Y with citations" does.
- **Letting the plan go stale.** A plan that isn't updated as work proceeds
  is worse than no plan — it actively misleads anyone reading it later.
  Call `plan_update` when status changes, not just at the finish.
- **Sequencing by narrative instead of dependency.** Two steps with no real
  dependency between them don't need to be strictly ordered — recognizing
  that is what makes parallel delegation possible.
- **Treating "blocked" as a dead end.** A `blocked` status with a clear
  `blocker` note is useful information, not a failure to hide — it tells
  whoever's watching exactly what needs to happen to unblock it.
- **Planning ceremony around a one-step task.** If there's nothing to
  sequence and nothing to delegate, skip straight to doing the work.

## Verification checklist

- [ ] The done condition and constraints were stated before steps were
      written
- [ ] Every step has a specific, checkable completion condition
- [ ] Dependencies reflect real ordering constraints, not just narrative
      order
- [ ] `plan_update` was called whenever a step's status genuinely changed
- [ ] Completed steps carry evidence, not just a status flip
- [ ] Blocked steps name their blocker explicitly
