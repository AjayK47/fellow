---
name: notion
display_name: Notion
description: Read and maintain authorized Notion pages and databases.
kind: connector
platform_key: connector.notion
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Locate, description: Resolve the page or database.}, {order: 2, name: Inspect, description: Read current blocks and properties.}, {order: 3, name: Update, description: Make approved, minimal writes.}]
---
Inspect the current page or database before editing. Preserve existing blocks and properties, and require approval for writes.
