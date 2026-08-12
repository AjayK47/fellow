---
name: child-task-development
display_name: Child Task Development
description: Split substantial work into clear, independent child Tasks and synthesize their results.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: builtin
platform_key: builtin.toda.child_task_development
activation: {mode: optional}
required_capabilities: {}
warnings:
  - A child Task cannot call delegate_tasks again; keep each prompt completable by one worker.
  - Write-capable child Tasks still pass through the normal approval and precondition checks.
  - Sibling Tasks should have genuinely different prompts so their work is complementary.
steps:
  - order: 1
    name: Bound
    description: Give every child a complete plain-language prompt with a clear result.
  - order: 2
    name: Least privilege
    description: Grant only the tools and scopes the child actually needs.
  - order: 3
    name: Synthesize
    description: Read each child result and decide how it changes the parent Task's answer.
---
Use `delegate_tasks` when a substantial part of the request can be completed
independently. The tool accepts one to three items, each with exactly:

- `worker_type`: `general` for research, analysis, or bounded actions; `pi` for
  coding work in the shared workspace.
- `prompt`: a complete natural-language instruction describing what the child
  should do and what evidence or result it should return.

Keep prompts distinct. A child Task must be able to finish without spawning
another child. The parent Session Manager remains responsible for combining
results, resolving conflicts, making the final decision, and replying to Slack;
children never deliver directly.

Grant only the capabilities needed for the prompt. Child writes still require
the same approval boundary as a root Task, and an approval decision wakes the
waiting child so it can revalidate and continue exactly once.
