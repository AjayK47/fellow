---
name: rest-graphql-debugging
display_name: REST and GraphQL Debugging
description: Diagnose API failures from request, response, schema, and service evidence.
kind: builtin
platform_key: builtin.hermes.rest_graphql_debugging
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
steps: [{order: 1, name: Reproduce, description: Capture the exact request and response.}, {order: 2, name: Compare, description: Check schema, auth, and service logs.}, {order: 3, name: Verify, description: Retest the smallest fix.}]
---
Capture method, URL, headers without secrets, payload shape, status, response body, and correlation IDs. Compare against the schema and service contract, then retest with a minimal safe change.
