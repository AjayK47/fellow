---
name: request-code-review
display_name: Request Code Review
description: Prepare a focused review request with scope, risk, and verification evidence.
kind: builtin
platform_key: builtin.hermes.request_code_review
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
steps: [{order: 1, name: Scope, description: Summarize changed behavior.}, {order: 2, name: Risk, description: Call out edge cases and migrations.}, {order: 3, name: Evidence, description: Include tests and review questions.}]
---
Give reviewers the intent, changed files or surfaces, risk areas, test evidence, and specific questions. Do not claim review approval or tests that were not performed.
