---
name: fastmcp
display_name: FastMCP Server Development
description: Design and validate a small MCP server with typed, least-privilege tools.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.fastmcp
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read, workspace.write]}
warnings:
  - Drafting server code with write_file does not install packages, start a process, or prove the server runs -- only a Pi child Task can actually execute Python here, and it may not be enabled.
  - Keep credentials and auth material out of workspace files entirely; write_file and edit_file already refuse writes containing obvious secret patterns, but design the server so it reads secrets from its own runtime environment rather than a checked-in file.
steps:
  - order: 1
    name: Contract
    description: Decide the tool surface -- names, typed schemas, auth model, side effects -- before writing a line of implementation.
  - order: 2
    name: Implement
    description: Draft narrow, typed, verb-named tools with write_file; keep the first slice small.
  - order: 3
    name: Validate
    description: Get the draft actually run and smoke-tested via a Pi child Task; do not claim it works without that.
---
# FastMCP Server Development

Design a small Python MCP server (using the `fastmcp` library) that exposes a
narrow, typed, least-privilege tool surface. This skill covers the *design and
drafting* work Toda can do directly; running, installing dependencies for,
or smoke-testing the server requires handing that part off, because Toda's
own tool surface has no shell and no package installer.

## What Toda can and cannot do here

- `read_file` / `write_file` / `edit_file` let you draft,
  read back, and edit the server's Python source, its dependency manifest,
  and its documentation, all inside the org's shared workspace folder.
- Nothing in Toda's own tool surface can run `pip install`, start the
  server process, or execute a client call against it. `run_code`'s sandbox
  only calls back into a fixed set of read-only company-data lookups
  (`memory_search` and similar) -- it cannot import the module you just wrote
  or reach the workspace filesystem at all.
- Actually running the server -- installing `fastmcp`, importing the module,
  listing its tools, calling one for real -- requires a Pi child Task:
  a full-trust subprocess with real network access, scoped to this same
  workspace folder. If that capability is not enabled for this run, say so
  plainly rather than asserting the server "works." A drafted-but-never-run
  server is a design document, not a validated one.

## When to use

- The task is to design a new MCP server (or a new tool on an existing one)
  wrapping an API, a read-only data store, or a file-processing workflow.
- The task is to review an MCP server's tool contracts for scope creep,
  missing validation, or overly broad side effects.

## When not to use

- The server already exists and only needs to be wired up to a running
  agent's configuration -- that's a deployment/config task, not a design
  task.
- The goal is ad-hoc access to someone else's already-running MCP server --
  there's nothing to design here.

## Procedure

### 1. Contract -- decide the surface before writing code

Pick the narrowest useful slice:

- **Wrapping an API** -- start with one read endpoint and one list/search
  endpoint, not the whole API surface.
- **Wrapping a database** -- expose read-only introspection (list tables,
  describe a table) plus one constrained, parameterized query path. Reject
  anything resembling a raw, unconstrained query string in the first
  version.
- **Wrapping file processing** -- expose deterministic operations
  (summarize, search within) with explicit path arguments, not an open-ended
  "run this" tool.

For every tool, decide up front:

- A concrete verb-based name (`get_customer`, `search_tickets`,
  `describe_table` -- not `run`, `process`, `do_thing`).
- A fully typed input schema.
- Whether it is read-only or has a side effect, and if so, what that side
  effect is and how it's bounded.
- Where auth material comes from at runtime (an environment variable read by
  the server process itself -- never a value written into a workspace file).

Add `@mcp.resource` only for stable, fetchable read content (schemas, policy
documents, generated reports) the client benefits from pulling directly.
Add `@mcp.prompt` only for a genuinely reusable prompt template tied to a
known workflow. Most servers need neither at first -- tools alone are
usually the right starting shape.

### 2. Implement -- draft with write_file

Write the server module, its dependency manifest (`pyproject.toml` or
`requirements.txt`), and a short README describing required environment
variables, using `write_file`. Keep each tool's docstring written as the
user-facing description a calling agent will see -- that description is part
of the contract, not internal commentary.

Design rules, independent of whether you can run it yet:

- Validate untrusted input at the top of the handler, before doing anything
  with it.
- Return structured, JSON-safe data.
- Centralize any outbound request logic in one helper rather than repeating
  header/auth handling per tool.
- Default new tools to read-only until there's a specific, named reason for a
  write.
- Cap result sizes (row counts, file preview length) rather than returning
  unbounded output.

### 3. Validate -- this step requires a Pi child Task

Design review alone (re-reading the schemas, docstrings, and validation
logic with `read_file`) catches contract problems but does not prove
the server runs. To actually validate it:

- Delegate a task that installs `fastmcp` and the server's other
  dependencies inside the workspace, imports the module, and lists its
  registered tools.
- Delegate a task that calls at least one real invocation of each new tool
  and reports the input, the output, and any error.
- If the server exposes HTTP transport, delegate a task that starts it
  locally and makes one real call against the running endpoint.

Word each delegated task narrowly and concretely (which file, which tool
name, which arguments to call it with, what output to report back) rather
than a vague "test the server" -- a Pi child Task is full-trust and
runs unsupervised except for its one hardcoded safety floor (no force-push,
no recursive deletes or path access outside the workspace), so a precise
task description is also how you keep its blast radius small.

If a Pi child Task is not available in this deployment, hand the
drafted server and its README back with an explicit note that it has been
designed and reviewed but not executed, and that running it requires either
enabling that capability or a human running it locally.

## Pitfalls

- **Claiming "it works" from a design read-through.** Only a real execution
  (via a Pi child Task) proves the server imports and its tools
  actually respond correctly.
- **Vague tool names.** `run`, `process`, `handle` tell a calling agent
  nothing about what the tool does or what happens when it's called.
- **One tool that does everything.** A single overloaded tool with a dozen
  optional parameters is harder to reason about and harder to gate than
  several narrow ones.
- **Auth material anywhere near a workspace file.** Design the server to
  read credentials from its own process environment; don't draft a config
  file containing one, even for local testing -- `write_file` will
  reject writes containing common token/key patterns, but the design intent
  matters independent of the scanner.
- **Skipping the resources-vs-tools-vs-prompts distinction.** Turning every
  document into a "prompt" or every data fetch into a "resource" when it's
  really an action is a common source of an unclear, hard-to-reason-about
  server.

## Verification checklist

- [ ] Every tool has a concrete verb-based name and a fully typed schema
- [ ] Read-only vs side-effecting is explicit for every tool
- [ ] Input validation happens before any external call or file operation
- [ ] No credential material is drafted into a workspace file
- [ ] At least one real invocation of each new tool was actually executed via
      a Pi child Task and its output reported -- or the draft is
      explicitly labeled as unexecuted
