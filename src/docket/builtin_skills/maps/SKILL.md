---
name: maps
display_name: Maps and Places
description: Research places, routes, and geographic facts with current evidence.
kind: connector
platform_key: connector.maps
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Resolve, description: Confirm place and geographic scope.}, {order: 2, name: Compare, description: Check current route or place details.}, {order: 3, name: Answer, description: State assumptions and source time.}]
---
Resolve ambiguous place names and current time-sensitive data before answering. State route assumptions, accessibility constraints, and source freshness.
