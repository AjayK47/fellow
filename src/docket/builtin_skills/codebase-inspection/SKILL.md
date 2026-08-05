---
name: codebase-inspection
display_name: Codebase Inspection
description: Build an evidence-backed map of an unfamiliar repository before editing it.
kind: builtin
platform_key: builtin.hermes.codebase_inspection
activation: {mode: optional}
required_capabilities: {all_of: [workspace.list, workspace.read]}
warnings:
  - The workspace folder is shared by the whole org, not private to this run -- list it before assuming it is empty or matches what you expect.
  - Cite the specific file and line for every claim; do not describe behavior you have not actually read.
steps:
  - order: 1
    name: Inventory
    description: List the workspace tree, find manifests, configuration, and entry points before reading anything deeply.
  - order: 2
    name: Trace
    description: Follow the one execution path that matters for the task, file by file, from entry point to effect.
  - order: 3
    name: Quantify
    description: Get size and language signal from what workspace_list and workspace_search actually return, or delegate for a real tool if precision matters.
  - order: 4
    name: Report
    description: State findings with file:line citations, and say plainly what remains unknown.
---
# Codebase Inspection

## Overview

Before changing or explaining a repository you have not worked in, build a map
of it from evidence, not from guessing at file names. This skill is the
discipline for that first pass: inventory the tree, find the entry points,
trace the one path that matters, and only then draw conclusions. Every
conclusion should be traceable to a specific file and, where it matters, a
specific line.

This is a reading skill. It does not authorize edits. If inspection turns up a
change that is clearly needed, say so and hand off to the appropriate skill
(`test-driven-development`, `systematic-debugging`, or a Pi child Task)
rather than patching files mid-inspection.

## Docket's tool surface for this

All of these operate on the one persistent workspace folder for this org --
not a private per-run scratch space, and not the org's actual production
repository unless something has explicitly checked that repository out into
the workspace first:

- `workspace_list` -- list files under a path, optionally recursive (capped at
  500 entries per call). Use this first, always. If the folder already has
  content from a previous task, that content is real and visible to you --
  read it before assuming you are starting from nothing.
- `workspace_read` -- read one UTF-8 text file, up to `max_chars` (default
  50,000, cap 200,000). Large generated files or binaries will not decode
  cleanly; skip them.
- `workspace_search` -- case-insensitive substring search across files under a
  path, optionally filtered by extension, returning up to 200
  `{path, line, text}` matches. This is your grep. It is line-oriented and
  literal-substring, not regex -- search for the exact token you expect
  (a function name, an error string, an import) rather than a pattern.
- `workspace_diff` -- unified diff between two files already in the workspace.
  Useful for comparing two versions of a config or a before/after pair someone
  else left there; not a git tool.

None of the above execute anything. There is no built-in way to count lines of
code, run a static-analysis binary, or clone a remote repository from inside
this skill. If the task genuinely needs one of those (a real language/LOC
breakdown, cloning a repo that is not already in the workspace, running a
linter), that requires a Pi child Task -- a full-trust subprocess
running the real coding CLI agent with network access, scoped to this same
workspace folder. It is only available when Phase D is enabled for this
deployment; if it is not, say so rather than fabricating a metric.

For a repository large enough that no one pass can hold it, use `delegate_tasks`
to fan out 2-3 independent child Tasks (for example: "map the API layer" and
"map the persistence layer" as separate prompts), each with its own
`workspace_read`/`workspace_search` grant and a tight token budget, then
synthesize their reports yourself.

## Procedure

### 1. Inventory

- `workspace_list` with `recursive: true` at the repository root (or a
  reasonable subpath if the tree is huge and the task is scoped).
- Read the manifest that tells you what kind of project this is:
  `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, or equivalent.
  These name the language, the dependency set, and often the entry point.
- Read the top-level README if one exists -- but treat it as a hypothesis
  about the code, not a fact about it. Names and docs drift from behavior.
- Identify configuration: env-driven settings, feature flags, anything that
  changes behavior without changing code. Note it even if you don't read it
  in full yet.

Completion criterion: you can name the language(s), the dependency manager,
and at least one concrete entry point (a `main`, a CLI script, an app
factory, a server bootstrap) before moving on.

### 2. Trace

Pick the one execution path the task actually needs -- not the whole
codebase. Follow it with `workspace_read` and `workspace_search`:

- Start at the entry point and read forward.
- When you hit a call into another module, `workspace_search` for where that
  symbol is defined, then `workspace_read` that file.
- Keep a running note of the chain (`entry -> module A -> module B -> effect`)
  so you can present it, not just remember it.
- Stop tracing once you reach the boundary the task cares about (a database
  call, an external API call, a return to the caller) -- don't keep pulling
  on the thread past the point where it stops being relevant.

Completion criterion: you can state the path end to end, file by file, with
no unexplained jump.

### 3. Quantify (only if the task needs a number)

`workspace_list` and `workspace_search` give you rough signal for free: file
counts per directory, how many files match a given extension, how many hits a
symbol gets. That is usually enough to answer "how big is this" or "how
common is this pattern" at the precision a Slack conversation needs.

If someone needs an exact, defensible line-of-code or language-percentage
number, say plainly that this requires a Pi child Task to run a real
analysis tool inside the workspace (subject to that capability being enabled
for this run) rather than approximating it yourself and presenting the
approximation as precise.

### 4. Report

State:

- What you were asked to find and the path you traced to find it.
- The specific files and lines that support each claim.
- What you did not check and why (out of scope, too large, no time-box left).
- Any change you'd recommend, framed as a recommendation for a follow-up
  skill or task -- not as an edit you made during inspection.

## Pitfalls

- **Trusting the name over the code.** A function called `validate_user`
  that doesn't actually validate anything is a real and common finding --
  don't let the name substitute for reading the body.
- **Reading everything.** Unscoped inspection of a large repo burns the
  context budget before you reach the part that matters. Scope to the task's
  execution path first; widen only if the answer demands it.
- **Forgetting the workspace is shared.** Files, notes, or partial clones left
  by an earlier task in this org are still there. `workspace_list` before
  assuming a clean slate, and don't silently overwrite something another task
  may still need.
- **Presenting a guess as a measurement.** If you did not run a real counting
  tool, say "approximately" and say how you estimated it.
- **Skipping vendored and generated code.** Don't spend the trace on
  `node_modules`, `vendor`, build output, or generated protobuf/GraphQL
  bindings unless the task is specifically about them.

## Verification checklist

- [ ] `workspace_list` was run before any claim about the tree's shape
- [ ] Every behavioral claim cites a `path` and, where relevant, a line
- [ ] The traced path is stated end to end, not just its endpoints
- [ ] Any quantitative claim is either backed by tool output or explicitly
      marked as an estimate
- [ ] Unknowns and out-of-scope areas are named, not silently omitted
