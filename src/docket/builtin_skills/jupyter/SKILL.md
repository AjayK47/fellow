---
name: jupyter
display_name: Jupyter Notebook
description: Analyze data in a reproducible notebook with explicit inputs and outputs.
kind: builtin
platform_key: builtin.hermes.jupyter
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read, workspace.write]}
warnings:
  - Docket has no live Jupyter kernel of its own -- workspace_read/workspace_write only let you author the .ipynb file's JSON; actually executing a cell requires a Pi child Task, and outputs you write yourself without running anything are not real results.
  - Never write cell outputs by hand and present them as if a kernel produced them -- if the notebook wasn't executed, say so.
steps:
  - order: 1
    name: Author
    description: Write the notebook's cells (markdown and code) as a structured .ipynb file with workspace_write.
  - order: 2
    name: Execute
    description: Run the notebook's cells for real via a Pi child Task, since Docket has no kernel of its own.
  - order: 3
    name: Verify
    description: Confirm outputs came from an actual run, preserve cell order and provenance, and report limitations.
---
# Jupyter Notebook

Produce a reproducible `.ipynb` notebook -- markdown narration plus code
cells with real, executed outputs -- for a data analysis or exploration
task. Docket does not run a persistent Jupyter kernel: there is no
`jupyter lab`, no REPL, and no way to keep Python state alive between tool
calls in the base tool surface. What Docket can do directly is author the
notebook's JSON structure; getting it actually executed is a separate,
explicit step.

## What's actually available

- `workspace_write` / `workspace_read` / `workspace_patch` operate on the
  `.ipynb` file as a plain UTF-8 text file (notebooks are JSON under the
  hood). You can create cells, edit a cell's source, and read back the
  current structure this way.
- Nothing in Docket's own tool surface executes a notebook cell.
  `run_code`'s sandbox is a real Python execution environment, but it can
  only call back into a fixed allow-list of read-only company-data lookups
  (`brain_search` and similar) -- it has no filesystem access to the
  workspace and cannot import or run notebook cells, and it is a poor
  substitute for this skill because state does not persist between
  `run_code` invocations the way it does inside one kernel.
- Actually running the notebook -- installing whatever the analysis needs,
  executing cells against real data, capturing real stdout/plots into cell
  outputs -- requires a Pi child Task: a full-trust subprocess with
  real network access, scoped to the workspace. This is Phase D
  functionality and may not be enabled for a given deployment; if it isn't,
  you can author the notebook but must say plainly that it has not been run.

## When to use

- The deliverable is explicitly a notebook (the user asked for one, or the
  analysis benefits from an interleaved narrative-plus-code artifact that
  will be read later).
- The task needs a durable, re-runnable record of an analysis, not just a
  one-off answer in chat.

## When not to use

- A quick one-off calculation that doesn't need to persist -- just answer
  directly, or use `run_code` if it genuinely needs to combine multiple
  company-data lookups.
- The task needs a live, interactive iterate-and-inspect loop across many
  small steps. Without a persistent kernel, each Pi child Task call
  is a fresh process; iterating that way is possible but comes with real
  latency and cost per round trip. Say so if the task's actual need is fast
  iteration, not a notebook artifact.

## Procedure

### 1. Author the notebook structure

Write a valid `.ipynb` (nbformat 4) file with `workspace_write`:

- Markdown cells narrate what the analysis is doing and why, ahead of the
  code that does it.
- Code cells contain one coherent step each -- load data, one transformation,
  one plot -- rather than one giant cell.
- Leave `outputs` empty and `execution_count` null for any cell you have not
  actually run. An unexecuted cell with fabricated output content is worse
  than an honestly empty one.
- Declare inputs explicitly: where the data comes from, what format it's in,
  any assumption the analysis depends on.

### 2. Execute for real

Delegate the actual run to `delegate_tasks` with a concrete
description: which notebook file, what environment it needs (packages to
install), and an instruction to execute it top to bottom and save outputs
back into the same file. A reasonable delegated task reads like: "install
pandas and matplotlib in the workspace environment, execute
`analysis.ipynb` cell by cell in order, save the executed outputs back into
the notebook file, and report any cell that errored."

If the delegated run reports an error partway through, do not silently
patch around it by hand-writing the expected output -- either fix the
authored cell and re-delegate the run, or report the failure and stop.

### 3. Verify

- Read the executed notebook back with `workspace_read` and confirm
  `execution_count` increases monotonically across cells and outputs are
  present where expected -- both are direct evidence the run actually
  happened rather than being fabricated.
- Preserve the original cell order; don't reorder cells after execution in a
  way that would make the saved execution counts misleading.
- Report what the notebook actually shows plus any limitation: data that
  wasn't available, a step that couldn't run, an assumption baked into the
  analysis.

## Pitfalls

- **Hand-writing outputs.** Filling in a cell's `outputs` field with
  plausible-looking numbers you did not compute is fabrication, not
  analysis. If it wasn't executed, leave the output empty and say so.
- **Treating `run_code` as a notebook kernel.** It has no filesystem access
  and no persistent state across calls -- it cannot stand in for actually
  running the notebook.
- **Assuming a Pi child Task is always available.** It's an optional,
  explicitly-enabled capability. Check before promising an executed
  notebook, and fall back to "authored but not yet run" when it's absent.
- **One cell doing five things.** Makes partial failures unreadable and
  makes the narrative harder to follow. One step per cell.
- **Forgetting the workspace is shared.** The notebook file lives in the
  same org-wide folder every other task uses; pick a name that won't
  collide with something already there.

## Verification checklist

- [ ] Every code cell with a filled-in output was actually executed via
      `delegate_tasks` -- not hand-written
- [ ] `execution_count` is present and monotonically increasing for executed
      cells
- [ ] Markdown cells explain what each code step is doing and why
- [ ] Any cell that errored during execution is reported, not silently
      dropped or patched around
- [ ] If execution wasn't possible in this deployment, the notebook is
      clearly labeled as authored-but-unexecuted
