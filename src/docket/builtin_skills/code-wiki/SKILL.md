---
name: code-wiki
display_name: Code Wiki
description: Produce maintainable documentation from inspected code and runtime contracts.
kind: builtin
platform_key: builtin.hermes.code_wiki
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
steps: [{order: 1, name: Gather, description: Inspect source and configuration.}, {order: 2, name: Explain, description: Document behavior and boundaries.}, {order: 3, name: Verify, description: Check links and examples.}]
---
Document what the code actually does, not what its name suggests. Include entry points, data flow, configuration, failure modes, examples, and ownership boundaries; flag inferred details.
