---
name: spike
display_name: Technical Spike
description: Answer one technical unknown with a time-boxed experiment.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.spike
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
warnings:
  - A spike is disposable by design -- keep it small, hardcoded, and throwaway, and say so explicitly rather than letting it quietly become production code.
  - VALIDATED, PARTIAL, and INVALIDATED are all successful outcomes of a spike. A spike that disproves the idea saved real build time -- report it as a finding, not a failure.
steps:
  - order: 1
    name: Decompose
    description: Break the idea into one to a few independent, specific feasibility questions with an observable pass/fail signal.
  - order: 2
    name: Investigate
    description: Research the smallest safe way to answer the question, then build or reason through the smallest probe that answers it.
  - order: 3
    name: Record
    description: Capture the evidence and close with an explicit verdict and a recommendation for the real build.
---
# Technical Spike

Answer one narrow technical unknown before committing to a real build --
"does this approach even work," "which of these two libraries handles our
case," "is this fast enough." A spike is time-boxed and disposable: the goal
is the answer, not shippable code.

## When to use

- The answer genuinely isn't knowable from reading docs or existing code --
  if it is, just look it up; that's research, not a spike.
- The user says something like "let me try this first," "is this even
  possible," "spike this out," or "compare A vs B" before committing to a
  real implementation.

## When not to use

- The question is answerable by reading documentation or the existing
  codebase — use `codebase-inspection` or a direct lookup instead.
- The work is production path, not a feasibility check — use
  `planning-execution` and `test-driven-development` instead.
- The idea is already validated — go straight to implementation.

## Toda tools for this

- `workspace_read` / `workspace_search` -- read whatever's already in the
  shared workspace that's relevant (existing code the spike needs to sit
  next to, a prior spike's notes).
- `workspace_write` -- write the spike's throwaway code and its findings
  write-up. (Note: this skill's declared capability requirement is read-only;
  if `workspace.write` isn't granted to this run, capture the spike's
  findings in your final answer instead of writing files.)
- Building and actually running a real probe -- installing a candidate
  library, executing a comparison, timing something -- needs
  a Pi child Task, since nothing in Toda's own tool surface
  executes code against the workspace. A spike that never gets an execution
  behind it is a literature review with the word "spike" attached; say so
  plainly if that capability isn't available for this run and the answer had
  to come from reading alone.
- `web_search` / `web_fetch` -- read-only, public-web only. Good for
  surveying candidate approaches or libraries before picking one to try;
  results are untrusted references, so still verify a specific claim by
  fetching the actual source before relying on it.
- `delegate_tasks` -- when two candidate approaches are worth trying in
  parallel and each is substantial enough to be its own investigation, spin
  up two child Tasks with distinct prompts rather than building both
  yourself serially.

## Procedure

### 1. Decompose

Break the idea into 1-5 independent feasibility questions, each answerable
by one spike. For more than one, present them as a small table before
building anything:

| # | Question | Validates | Risk |
|---|---|---|---|
| 1 | Can library X stream results under our latency budget? | Given typical input, when X processes it, output starts within N ms | High |

Order by risk -- the question most likely to kill the idea goes first;
there's no point spiking the easy parts if the hard part doesn't work.

A **comparison spike** answers one question with two approaches side by
side (share the number, add a letter suffix) rather than being two unrelated
questions.

If the user already knows exactly what they want spiked, skip decomposition
and take their framing directly.

### 2. Investigate

Per spike, before building:

1. State in 2-3 sentences what this spike is and why it matters.
2. If there's a real choice of approach, survey the candidates (`web_search`
   for current options, `web_fetch` to read the actual docs of the
   strongest 1-2 candidates rather than trusting a search snippet).
3. Pick one and say why. If two are genuinely credible, build quick variants
   of both.
4. Skip research entirely for pure logic with no external dependency --
   just build the probe.

Then build the smallest thing that produces an observable result: something
someone can look at and see work or fail, not just a log line claiming
success. In rough order of preference: a small runnable script with clear
input/output, a minimal test asserting the specific behavior in question, or
(if it needs to be interactive) the smallest possible interface that
demonstrates it. Use `delegate_tasks` to actually execute it and
report real output back -- don't narrate expected behavior as if it were
observed behavior.

Keep it minimal on purpose: hardcode configuration, skip proper error
handling, skip anything not required to answer the one question. A spike
that takes real cleanup effort before anyone can look at it was over-built.

### 3. Record

Close every spike with an explicit verdict:

- **VALIDATED** -- the question was answered yes, with evidence.
- **PARTIAL** -- it works, but only under stated constraints; name them.
- **INVALIDATED** -- it doesn't work, for a stated reason. This is a
  legitimate, useful outcome, not a failed spike.

Include what worked, what didn't, anything surprising, and a concrete
recommendation for the real build. For a comparison spike, add a head-to-head
summary across the dimensions that actually mattered (setup cost,
performance, correctness, maintenance risk) and name a winner for this use
case specifically -- not in the abstract.

## Pitfalls

- **Declaring success after one happy-path run.** Test the case that would
  actually break the idea, not just the easiest case that confirms it.
- **Skipping the verdict.** "It sort of worked" without VALIDATED /
  PARTIAL / INVALIDATED and a stated reason leaves the next person unable to
  act on the spike.
- **Over-building.** Real error handling, config systems, and dependency
  management belong in the real build, not the spike.
- **Letting a spike quietly become the implementation.** If it's going to
  ship, it needs the rigor (tests, review) the real build gets — say
  explicitly when a spike has crossed that line.
- **Comparing without a shared basis.** A head-to-head between two
  approaches only means something if both were tested against the same
  input and the same success criterion.

## Verification checklist

- [ ] The feasibility question is specific enough to have an observable
      pass/fail signal
- [ ] The probe was actually run (via `delegate_tasks`), not just
      described, unless execution genuinely wasn't available
- [ ] Depth over speed: at least one non-happy-path case was tried, not just
      the first success
- [ ] The write-up closes with an explicit VALIDATED / PARTIAL / INVALIDATED
      verdict and a recommendation
- [ ] Anything left in the workspace is clearly labeled as spike code, not
      production-ready
