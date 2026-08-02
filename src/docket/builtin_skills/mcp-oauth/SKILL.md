---
name: mcp-oauth
display_name: MCP OAuth Gateway
description: Connect an MCP server with scoped, expiring, revocable OAuth credentials.
kind: builtin
platform_key: builtin.hermes.mcp_oauth
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Authorize, description: Request only required scopes.}, {order: 2, name: Store, description: Protect expiry and refresh metadata.}, {order: 3, name: Revoke, description: Handle expiry, denial, and revocation.}]
---
Request least-privilege scopes, keep tokens out of prompts and logs, track expiry and refresh state, and handle revocation or denial as normal states that require reauthorization.
