---
name: code-wiki
display_name: Code Wiki
description: Produce maintainable documentation from inspected code and runtime contracts.
kind: builtin
platform_key: builtin.hermes.code_wiki
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
warnings:
  - Every diagram node and every documented function must correspond to code you actually read; fabricated structure is the single biggest failure mode of generated docs.
  - Writing the wiki into the org's shared workspace makes it visible to every other task in this org -- pick the output path deliberately.
steps:
  - order: 1
    name: Gather
    description: Inventory the repository and read manifests, entry points, and the modules you intend to document.
  - order: 2
    name: Explain
    description: Write what the code actually does -- entry points, data flow, configuration, failure modes, boundaries.
  - order: 3
    name: Diagram
    description: Add Mermaid diagrams only where they clarify structure a real reader would otherwise have to reconstruct from prose.
  - order: 4
    name: Verify
    description: Check every link, path, and diagram node resolves to a real file you read.
---
# Code Wiki

Produce reference documentation (what the code does and how it's put
together) for a repository sitting in Docket's shared workspace. This is not
a shell-driven wiki generator -- there is no `git clone`, no terminal, and no
diagramming CLI available directly to this skill. Everything here is built
from `workspace_read` / `workspace_search` / `workspace_list` output, written
out with `workspace_write`.

This produces reference documentation, not a strategic "why does this
project exist" narrative. If the request is really for the latter, that's a
different, shorter piece of writing -- don't force it into this shape.

## When to use

- Someone asks to document a codebase already checked into the shared
  workspace, or wants a structured onboarding reference for it.
- The output needs to be a stable artifact (markdown, optionally with Mermaid
  diagrams) rather than an inline chat answer.

## When not to use

- Documenting one file or one function -- just read it with `workspace_read`
  and answer directly.
- The repository is not yet in the shared workspace and getting it there
  requires cloning from a remote URL. Cloning needs real network access,
  which only a Pi child Task has -- either delegate the whole
  documentation task to it (if the task is substantial enough to hand off
  wholesale) or first delegate a narrower "clone this repo into the
  workspace" task, then come back and use this skill against the checked-out
  files.
- Strategic "why this exists" narrative -- write that directly, it isn't a
  wiki.

## Docket tools for this

- `workspace_list` (recursive) -- scan directory structure, cap depth and
  breadth deliberately; a 500-file wall-of-output is not useful signal.
- `workspace_read` -- read manifests (`pyproject.toml`, `package.json`,
  `Cargo.toml`, `go.mod`), the README, and the source files for each module
  you commit to documenting. Use `max_chars` to avoid pulling in huge
  generated files.
- `workspace_search` -- find where a symbol is defined or referenced instead
  of guessing a file name; also useful for a rough "how many places call
  this" signal when picking which modules matter most.
- `workspace_write` -- write the generated markdown files. One file per
  logical page (`README.md`, `architecture.md`, `modules/<name>.md`,
  `diagrams.md`) rather than one giant document; writes are capped at 1 MB
  each and 10 MB total for the whole workspace, and are scanned for obvious
  secret patterns.
- `delegate_tasks` -- for a repository large enough that documenting 8-10
  modules single-threaded would blow the context budget, split module
  write-ups across 2-3 child Tasks with distinct prompts, then assemble
  the pages yourself so cross-links stay consistent.

## Procedure

### 1. Gather

- `workspace_list` recursively, capped at a sensible depth. Note top-level
  directories and obvious noise (dependency directories, build output,
  generated code) to exclude from the documentation pass.
- `workspace_read` the manifest and README.
- Pick 6-10 modules to document in the first pass, prioritized by: how many
  other places reference them (via `workspace_search` hit count), size, and
  README mentions. State the module list before writing anything if the
  repository is large -- it gives whoever asked a chance to redirect before
  you spend the budget.

### 2. Explain

For each chosen module, read its 3-5 most load-bearing files (largest, named
like `core`/`main`/an `__init__`, or most-referenced) and write:

- **Purpose** -- one or two sentences, in the module's own domain language,
  not "this module is responsible for handling things."
- **Public surface** -- the functions, classes, and constants other code
  actually uses. Show signatures, not full implementations.
- **Internal structure** -- how it's organized, where state lives.
- **Dependencies** -- what it's used by, what it uses.
- **Notable gotchas** -- anything a new reader would trip over.

At the top level, write an overview page: what the project is, its entry
points, and a module map linking to each per-module page. Write an
architecture page describing what talks to what and where data enters and
leaves the system.

### 3. Diagram (optional, only where it earns its place)

Mermaid renders natively in most markdown viewers that will host this wiki.
Use it sparingly:

- A `flowchart` for the system-level architecture (cap at roughly 20 nodes;
  split into more than one diagram past that).
- A `classDiagram` for the 5-10 most central types, if the language has
  classes; skip it for a purely functional or struct-based codebase rather
  than forcing an awkward fit.
- A `sequenceDiagram` for 2-4 of the most important workflows, tracing a real
  call path you actually followed through the code.

Every box and arrow in a diagram must correspond to something you read. Do
not add a participant or a call edge because it seems structurally likely.

### 4. Verify

- Every internal link (to another wiki page, or to a source file path)
  resolves to something that exists.
- Every diagram node names a real component you can point to in the source.
- Module count matches what you told the requester you'd cover, or you
  explained the change.
- No page describes tests, vendored dependencies, or generated code as if it
  were hand-written product code.

## Pitfalls

- **Fabricating components.** The failure mode that makes generated docs
  worthless is plausible-sounding invention. If you didn't read it, don't
  document it.
- **Generic AI prose.** "This module handles X" restates the module name and
  tells the reader nothing they didn't already know.
- **Restating code as prose instead of explaining it.** A description that
  just narrates the code line by line is worse than a link to the function.
- **Skipping the module list preview on a large repo.** Committing to 40
  modules of documentation without checking in first wastes budget if only 5
  were wanted.
- **Writing into the wrong place.** This workspace is shared across the
  whole org's tasks. Pick an output path that won't collide with or bury
  something another task is using, and say where you wrote it.
- **Oversized diagrams.** Past ~20-30 nodes, Mermaid stops being legible.
  Split into sub-diagrams instead of cramming.

## Verification checklist

- [ ] Module list was stated up front for any repository large enough that
      scope needed confirming
- [ ] Every documented function/class/module was actually read via
      `workspace_read`, not inferred from its name
- [ ] Every diagram node maps to real code
- [ ] Output files were written with `workspace_write` to a stated,
      deliberate path
- [ ] Tests, vendored code, and generated files are excluded or clearly
      labeled as such
