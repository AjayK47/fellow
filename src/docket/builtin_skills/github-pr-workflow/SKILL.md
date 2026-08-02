---
name: github-pr-workflow
display_name: GitHub Pull Request Workflow
description: Prepare and manage a pull request through bounded, reviewable steps.
kind: connector
platform_key: connector.github.pr_workflow
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio, workspace.read]}
steps: [{order: 1, name: Prepare, description: Verify branch, diff, tests, and description.}, {order: 2, name: Review, description: Surface risk and required approvals.}, {order: 3, name: Deliver, description: Create or update only after approval.}]
---
Inspect branch and diff, run available checks, write a concise PR description, and surface risk. Creating, updating, merging, or commenting requires explicit approval.
