---
name: fastmcp
display_name: FastMCP Server Development
description: Design and validate a small MCP server with typed, least-privilege tools.
kind: builtin
platform_key: builtin.hermes.fastmcp
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read, workspace.write]}
steps: [{order: 1, name: Contract, description: Define tool schemas and auth.}, {order: 2, name: Implement, description: Keep handlers narrow and typed.}, {order: 3, name: Test, description: Exercise errors and authorization.}]
---
Keep MCP tools narrow, typed, permission-aware, and explicit about side effects. Validate schemas, error paths, authentication, and resource limits before exposing a server.
