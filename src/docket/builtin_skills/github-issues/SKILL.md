---
name: github-issues
display_name: GitHub Issues
description: Triage and maintain GitHub issues with reproducible evidence.
kind: connector
platform_key: connector.github.issues
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Triage, description: Classify scope and severity.}, {order: 2, name: Clarify, description: Ask for missing reproduction details.}, {order: 3, name: Update, description: Make only approved issue changes.}]
---
Search before creating duplicates. Preserve reporter intent, classify severity and component from evidence, and require approval before public writes or state changes.
