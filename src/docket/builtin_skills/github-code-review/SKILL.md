---
name: github-code-review
display_name: GitHub Code Review
description: Review GitHub changes for correctness, security, and regression risk.
kind: connector
platform_key: connector.github.code_review
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Read context, description: Inspect the PR and related code.}, {order: 2, name: Check behavior, description: Look for concrete defects and missing tests.}, {order: 3, name: Report, description: Give prioritized findings with evidence.}]
---
Review the actual diff and surrounding code. Report only actionable findings, ordered by severity, with file/line evidence and a clear explanation of impact. Separate questions from defects.
