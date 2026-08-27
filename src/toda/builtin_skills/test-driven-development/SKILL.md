---
name: test-driven-development
display_name: Test-Driven Development
description: Implement behavior through a failing test, minimal change, and verified refactor.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.tdd
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read, workspace.write]}
warnings:
  - Toda cannot execute a test itself -- write_file only edits files. Watching a test actually fail and then actually pass requires a Pi child Task; without one, this skill can draft the RED/GREEN/REFACTOR cycle but cannot prove any step happened.
  - Never report a test as passing or failing without a real execution behind the claim.
steps:
  - order: 1
    name: Red
    description: Write one focused failing test with write_file, then get it actually run and confirm it fails for the right reason.
  - order: 2
    name: Green
    description: Write the smallest implementation that makes that one test pass, then get it actually run and confirm it does.
  - order: 3
    name: Refactor
    description: Clean up structure with tests still green, re-running after each change; repeat the cycle for the next behavior.
---
# Test-Driven Development

Write the test before the code, watch it fail for the right reason, then
write the smallest change that makes it pass. The discipline only works if
"watch it fail" and "watch it pass" are real observations, not assumptions --
which in Toda means every RED and every GREEN checkpoint has to go through
an actual execution.

**If you didn't watch the test fail, you don't know if it tests the right
thing.**

## Toda's execution reality

`write_file` and `edit_file` let you author and edit test files
and implementation files directly in the org's shared workspace. Neither one
runs anything. Toda has no built-in test runner wired up yet -- the
`code_test` tool exists in the schema but currently always reports that its
runtime isn't configured, so it cannot be relied on to execute anything.

The only way to get a real pass/fail signal in this harness is
`task_delegate`: a Pi child Task that can install
dependencies and run the project's real test command inside the shared
workspace. It is an optional, explicitly-enabled capability (Phase D) and may
not be present in every deployment.

If it is not available for this run: draft the test and the implementation
with `write_file`, describe the RED/GREEN cycle you intend, and say
plainly that no execution has actually happened rather than asserting the
test failed or passed. A described-but-unrun cycle is a plan, not TDD.

## When to use

**Default to this for:** new features, bug fixes, refactors, and any
behavior change in a codebase that has a test suite.

**Ask first before skipping, for:** throwaway prototypes, generated code, or
pure configuration changes where a human has said tests aren't warranted.
"Skip it just this once" for anything else is the rationalization this skill
exists to catch.

## The cycle

### RED -- write one failing test

Write the smallest test that demonstrates one behavior, with a name that
describes the behavior (not the implementation). If the test's name needs
"and" to describe what it checks, split it into two tests.

Prefer real code over mocks. A test built entirely from mocked collaborators
verifies that the mocks were called, not that the behavior works.

Then get it run for real, via `task_delegate` ("run this one specific
test and report the exact output"), and confirm:

- It fails -- and fails because the behavior is missing, not because of a
  typo or an import error.
- The failure message is the one you expected.

A test that passes immediately on first run is testing something that
already exists, not the behavior you're adding -- fix the test before going
further. A test that errors instead of failing needs the error fixed and
re-run until it fails for the intended reason.

### GREEN -- the smallest implementation that passes

Write the least code that makes the one new test pass. Cheating is
acceptable here on purpose -- hardcoded return values, obvious duplication,
skipped edge cases. Refactor comes next; don't reach for it early.

Get it run for real via `task_delegate`: the one new test, then the
full relevant suite, to confirm both the new behavior and no regression.
Only trust a "tests pass" claim that came from an actual run reported back
to you.

### REFACTOR -- clean up with tests staying green

Once green, remove duplication, improve names, extract helpers -- without
adding new behavior. Re-run the suite via `task_delegate` after each
structural change. If a change breaks a test, undo it and take a smaller
step rather than debugging the refactor and the behavior at the same time.

### Repeat

One behavior, one RED-GREEN-REFACTOR cycle, then the next. Resist writing a
pile of tests up front and then implementing against all of them at once --
that produces brittle tests designed before the implementation taught you
what the interface actually needs to look like. Take one vertical slice
(test then implementation) at a time.

## Common rationalizations, and why they don't hold here

| Excuse | Reality |
|---|---|
| "I'll write the test after, to verify it works" | A test written after the code passes immediately -- that proves nothing about whether it tests the right thing. |
| "I already checked it manually" | There's no record of what was checked and no way to re-run it after the next change. |
| "It's simple enough to skip" | Simple code breaks in simple ways; the test costs little and catches those. |
| "I can't actually run tests right now, I'll just say it passed" | Don't. If a Pi child Task isn't available, say the cycle is drafted but unverified -- never report a result you didn't observe. |

## Pitfalls

- **Reporting a pass or fail you didn't observe.** The single most important
  rule in this environment specifically: Toda's own tools cannot execute
  anything, so any claim about test results must trace back to a real
  `task_delegate` execution.
- **Writing implementation before the test exists.** If it happens, delete
  the implementation and start the cycle over -- don't keep it "as
  reference" while writing the test around it.
- **Skipping the full-suite run after GREEN.** A test that passes in
  isolation but breaks something else is a regression you just introduced
  silently.
- **Bundling multiple behaviors into one RED-GREEN cycle.** Makes it
  impossible to tell which behavior a failure belongs to.
- **Testing mocked interactions instead of real behavior.** Verify what the
  code does, not that a mock was called in the expected order.

## Verification checklist

- [ ] Every new function/behavior has a test that was written before its
      implementation
- [ ] Each test's failure was actually observed (via `task_delegate`)
      before the implementation was written
- [ ] Each test's pass was actually observed after the implementation
- [ ] The full relevant suite was re-run and confirmed clean after each
      change, not just the one new test
- [ ] If no execution capability was available, the result is reported as
      "drafted, unverified" rather than as pass/fail
