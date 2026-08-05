---
name: product-dogfooding
display_name: Product Dogfooding
description: Test a product through realistic user journeys and record actionable failures.
kind: builtin
platform_key: builtin.hermes.product_dogfooding
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
warnings:
  - Docket has no built-in browser -- exercising a web UI as a user would requires a Pi child Task to actually drive one; without it, this skill can only exercise an API-shaped surface directly.
  - Record exact steps and exact observed behavior, not a vague impression -- a report a developer can't reproduce from isn't actionable.
steps:
  - order: 1
    name: Journey
    description: Define a realistic, specific user path through the product, not an abstract feature list.
  - order: 2
    name: Exercise
    description: Actually walk the journey using real inputs and state transitions, capturing evidence at each step.
  - order: 3
    name: Report
    description: Record expected versus observed behavior, severity, and a reproducible follow-up for every finding.
---
# Product Dogfooding

Walk a real user journey through a product end to end, the way an actual
user would, and record exactly what breaks -- with evidence, not impression.
The value of dogfooding comes from realistic state transitions (a real
signup, a real search, a real multi-step flow) rather than isolated,
disconnected checks.

## What Docket can actually exercise

Docket's own tool surface has no browser control -- there is no click,
type, or screenshot tool built in. What's directly available depends on how
the product being tested is reachable:

- **If the product is reachable through a connected integration** (Composio),
  `composio_search` finds available actions for that connected application
  and `composio_describe` shows an action's exact schema before calling it.
  Calling the action itself uses the specific tool that search surfaces --
  never guess an action's name or arguments. This lets you exercise a real
  user journey against an actual connected product (create a ticket, send a
  message, update a record) with real side effects, which is a legitimate
  and often the most direct form of dogfooding available here.
- **If the product is a web UI with no API-shaped connector**, actually
  clicking through it requires a real browser, which only exists inside
  a Pi child Task -- a full-trust subprocess with real network
  access, scoped to the shared workspace, available only when Phase D is
  enabled. Delegate a task that drives a headless browser through the
  specific journey and reports back console errors, response codes, and
  screenshots saved into the workspace. Without that capability enabled,
  say plainly that a UI journey can't be exercised directly and either
  narrow the test to whatever is reachable another way, or ask for a human
  to walk the flow and report what they see.
- **If the product exposes Docket's own Slack surface** (a bot interaction,
  a notification, a button), `slack_send_message` and `slack_react` let you
  exercise that flow directly as a real message in the conversation.

Pick the path that matches how the product is actually reachable -- don't
force a Composio-shaped test onto a pure UI flow, or vice versa.

## When to use

- A specific, realistic user journey needs to be validated before or after a
  change, and the goal is finding real breakage, not confirming a
  hypothesis.
- QA needs recorded evidence (not just "it worked when I tried it") for a
  flow that matters.

## When not to use

- The question is about one narrow function's behavior -- read the code or
  call it directly instead of framing it as a user journey.
- No real path exists yet to exercise the product (no connector, no browser
  capability, and no human available to walk it) -- say so rather than
  fabricating a result.

## Procedure

### 1. Define the journey

State the journey as a sequence of real actions and expected outcomes, not
an abstract feature name: "a new user signs up, receives the confirmation
message, and successfully completes their first action" rather than
"test onboarding." Include the edge cases worth trying alongside the happy
path: an empty submission, an invalid input, a repeated action.

### 2. Exercise the journey

Walk it step by step using the tool path that matches how the product is
reachable (see above). At each step:

- Use realistic input -- not a placeholder value that would never occur in
  real use.
- Capture the actual response: the action's returned result, an error
  message, a screenshot, a console error, a Slack message actually
  delivered.
- Note the state transition: did the system end up where a real user would
  expect after this step, before moving to the next one.

### 3. Report

For each issue found:

- **Steps to reproduce** -- exact enough that someone else can walk the same
  path and hit the same result.
- **Expected vs. actual** -- stated separately and specifically, not merged
  into one vague sentence.
- **Evidence** -- the actual response, error, or screenshot path, not a
  paraphrase of it.
- **Severity** -- critical (blocks the core journey), high (breaks a real
  but non-blocking part of it), medium, or low (cosmetic/minor).

De-duplicate before finalizing: several observed symptoms that are really
one underlying bug should be reported once, with all the places it showed
up noted together. Sort by severity, and close with what was and wasn't
tested plus any blocker that limited coverage.

## Pitfalls

- **Vague impressions instead of reproducible steps.** "Something felt
  off about checkout" isn't actionable; the exact input, exact response,
  and exact expected-vs-actual is.
- **Testing only the happy path.** The interesting bugs are usually in
  empty states, invalid input, and repeated/rapid actions.
- **Fabricating a browser-driven result without a Pi child Task.**
  If a UI journey genuinely couldn't be exercised in this run, say that
  plainly rather than describing an inferred outcome as if it were
  observed.
- **Guessing a connector action's name or arguments.** Always
  `composio_search`/`composio_describe` first; a guessed action slug will
  simply fail, or worse, silently call the wrong thing.
- **Reporting duplicate symptoms as separate bugs.** Merge them, and note
  every place the same root issue showed up.

## Verification checklist

- [ ] The journey is stated as a concrete sequence of real actions, not an
      abstract feature name
- [ ] Each step used realistic input and captured real evidence of the
      response
- [ ] Both happy-path and at least one edge case were exercised
- [ ] Every reported issue has reproducible steps, expected vs. actual, and
      evidence
- [ ] Duplicate symptoms are merged into one issue rather than reported
      separately
- [ ] Coverage gaps and blockers are stated explicitly, not silently omitted
