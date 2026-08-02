---
name: airtable
display_name: Airtable
description: Query and update authorized Airtable bases with explicit record scope.
kind: connector
platform_key: connector.airtable
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Identify, description: Confirm base, table, and fields.}, {order: 2, name: Query, description: Read the minimal record set.}, {order: 3, name: Mutate, description: Apply approved changes and verify.}]
---
Confirm base, table, fields, filters, and record scope before action. Avoid broad updates and require approval for mutations.
