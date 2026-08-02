---
name: github-repository-management
display_name: GitHub Repository Management
description: Maintain repository metadata and workflows with guarded writes.
kind: connector
platform_key: connector.github.repository_management
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Inspect, description: Read current repository state.}, {order: 2, name: Plan, description: Describe the intended metadata change.}, {order: 3, name: Apply, description: Make the approved change and verify it.}]
---
Read the current repository state and explain the exact proposed mutation before applying it. Never delete, merge, change permissions, or alter branch protections without approval.
