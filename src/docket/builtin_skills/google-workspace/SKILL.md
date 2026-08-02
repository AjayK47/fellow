---
name: google-workspace
display_name: Google Workspace
description: Search and update authorized Google Workspace content through connected tools.
kind: connector
platform_key: connector.google.workspace
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Resolve, description: Identify the correct account and object.}, {order: 2, name: Read, description: Inspect permission-safe content.}, {order: 3, name: Write, description: Apply approved changes and verify.}]
---
Use only the connected account and exact action schema returned by the connector. Search before creating duplicates, preserve sharing boundaries, and require approval for writes.
