---
name: systematic-debugging
display_name: Investigate Before Fixing
description: Investigate issues from evidence before recommending or applying a fix.
version: "1.0.0"
license: Apache-2.0
origin:
  project: toda
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.systematic_debugging
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Separate observed facts from assumptions -- a hypothesis is not evidence until you have watched it get tested.
  - A fix that has not actually been re-run and confirmed is not verified; do not report a fix as working if execution wasn't possible in this deployment.
  - Three failed fix attempts on the same symptom is a signal to question the design, not to try a fourth patch.
steps:
  - order: 1
    name: Establish the symptom
    description: Restate the failure in testable terms -- what fails, how it fails, and under what conditions.
  - order: 2
    name: Gather evidence
    description: Read the actual error, recent changes, and relevant code before forming any hypothesis.
  - order: 3
    name: Form and test hypotheses
    description: Rank plausible causes, test the most likely one with the smallest possible check, and don't stack fixes.
  - order: 4
    name: Verify the fix
    description: Confirm the fix against the original symptom for real, state what was tested, and name remaining uncertainty.
---
# Investigate Before Fixing

Guessing at a fix wastes time and often creates a second bug alongside the
first. This skill is the discipline for finding the root cause before
touching anything: read the actual error, gather real evidence, rank
hypotheses, test the cheapest one first, and only then change code.

**No fix without a root cause established first.** If you haven't finished
gathering evidence, you aren't ready to propose a fix yet -- and if you
can't finish gathering evidence, say what's missing rather than filling the
gap with a guess.

## Toda's evidence-gathering tools

- `workspace_read` / `workspace_search` -- read the failing code, the error
  path, and anything nearby. `workspace_search` is a literal substring
  search (not regex): search for the exact error string, function name, or
  variable to find where it's referenced.
- `workspace_diff` -- compare two versions of a file already in the
  workspace, useful when someone has left a before/after pair to inspect.
- `delegate_tasks` -- for a bug that spans multiple independent components
  (a request path through several services, or several candidate causes
  worth investigating in parallel), fan out 2-3 child Tasks with distinct
  investigation prompts and synthesize their findings yourself. This
  does not replace forming your own ranked hypotheses -- it's for covering
  more ground faster, not for skipping the analysis.
- `delegate_tasks` -- the way to hand execution to a Pi child Task:
  a failing test, reproduce a crash, add temporary diagnostic output and
  observe it, or check out and run a specific prior commit. It's a
  full-trust subprocess with real network and package-install access,
  scoped to the shared workspace, and only available when Phase D is
  enabled. Root-cause investigation of a live bug almost always needs at
  least one real execution somewhere in the loop -- if this capability isn't
  available, say plainly that you can reason from static reading only and
  cannot confirm a hypothesis by reproducing it.

There is no built-in way to inspect live production logs, a running
process's memory, or a database directly from this skill -- if the bug
needs that kind of live telemetry, say so and ask what evidence the human
can pull, rather than speculating about runtime state you cannot observe.

## Phase 1: Establish the symptom and gather evidence

Before forming any theory:

1. **Read the error completely.** Full stack trace, exact message, line
   numbers, error codes. Errors usually contain most of the answer already
   — don't skim past them.
2. **Build the tightest reproduction you can.** Can this symptom be
   triggered on demand? A failing test at the right seam is the strongest
   form; a scripted repro against fixture input is the next best. If
   `delegate_tasks` is available, use it to hand that execution to a Pi child Task
   reproduction and confirm it goes red on this bug specifically -- not
   just "something failed," but this exact symptom. If the bug is
   intermittent, the immediate goal is raising the reproduction rate high
   enough to debug, not achieving perfection on the first try.
3. **Check what changed recently.** Read recent history in the affected
   files, note new dependencies or config changes, and treat "nothing
   changed" as a claim to verify, not an assumption to accept.
4. **In a multi-component system, gather evidence at each boundary** before
   theorizing about which component is at fault. What enters a component,
   what leaves it, whether config actually propagated the way it was
   supposed to.
5. **Trace bad data to its source.** When a bad value shows up deep in a
   call stack, keep tracing upstream (via `workspace_search` for callers)
   until you find where it originates. Fix at the source, not where the
   symptom happened to surface.

**Phase 1 is done when:** the error is fully read and understood, a
reproduction exists (or its absence is explicitly acknowledged), recent
changes have been reviewed, and you can state a root-cause hypothesis in
one sentence with the evidence behind it.

## Phase 2: Pattern analysis

- **Minimize the reproduction.** Once you have something that reliably
  fails, strip it down one element at a time -- cut inputs, callers,
  config -- re-checking after each cut, until removing anything else makes
  it stop failing. What's left is the minimal case, and it often becomes
  the regression test later.
- **Find a working comparison.** Is there similar code elsewhere in the same
  codebase that works? `workspace_search` for the pattern and read it fully
  — every line, not a skim — before comparing.
- **List every difference**, however small it looks. Don't pre-filter "that
  can't matter" before checking.

## Phase 3: Hypothesis and testing

- **Generate 3-5 plausible causes before testing any of them.** Rank by
  likelihood and by how cheap each is to falsify. For each, state the
  prediction it makes: "if X is the cause, changing Y should produce Z."
  Discard any hypothesis that doesn't make a checkable prediction.
- **Test the top-ranked one with the smallest possible probe.** Change one
  variable at a time. If you add temporary diagnostic output while probing,
  tag it with a distinctive marker so cleanup is a single search-and-remove
  afterward.
- **Confirmed?** Move to Phase 4. **Not confirmed?** Form a new hypothesis —
  don't layer a second fix attempt on top of the first without understanding
  why it didn't work.
- **Genuinely stuck?** Say so directly rather than pretending confidence,
  and ask for the piece of evidence that would unblock you.

## Phase 4: Implementation and the rule of three

1. Where feasible, create a failing regression test for the root cause first
   — see `test-driven-development` for the RED/GREEN discipline, including
   its same caveat that Toda needs a Pi child Task to actually
   execute anything.
2. Make one change addressing the root cause. No bundled refactoring, no
   "while I'm here" improvements.
3. Verify the fix against the original reproduction, for real, plus the
   broader relevant test suite if one exists and can be run.
4. **If the fix doesn't resolve it:** stop and count attempts. Fewer than
   three: return to Phase 1 with what you just learned. Three or more: stop
   proposing further patches and raise whether the underlying design is
   sound — a pattern of each fix revealing a new problem somewhere else, or
   each fix needing broader changes than the last, means the issue is
   architectural, not a bug to patch around.

## Pitfalls

- **Proposing a fix before Phase 1 is complete.** The single most common way
  this skill gets skipped under time pressure — and pressure is exactly
  when guessing produces the most rework.
- **Fixing the symptom instead of the source.** A null check where the bad
  value surfaces, instead of wherever it originated, buys a quieter crash
  and the same bug elsewhere later.
- **Stacking fixes.** Changing several things at once makes it impossible to
  know which change actually mattered, and easy to introduce something new.
- **Declaring victory on the happy path.** Confirm the fix against the
  original symptom specifically, plus edge cases nearby it.
- **Claiming a fix is verified without running anything.** If
  a Pi child Task wasn't available to actually re-run the
  reproduction, say the fix is a reasoned hypothesis pending real
  verification — not a confirmed resolution.

## Verification checklist

- [ ] The error/symptom was read in full, not skimmed
- [ ] A reproduction was attempted, and its success or failure to reproduce
      is stated explicitly
- [ ] At least 3 hypotheses were considered and ranked before testing any
- [ ] The chosen fix addresses the traced root cause, not just where the
      symptom appeared
- [ ] The fix was actually re-run against the original symptom (or the
      report explicitly says it wasn't, and why)
- [ ] If three or more fix attempts failed, the architecture was questioned
      rather than attempting a fourth patch
