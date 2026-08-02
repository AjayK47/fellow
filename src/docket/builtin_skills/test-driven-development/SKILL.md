---
name: test-driven-development
display_name: Test-Driven Development
description: Implement behavior through a failing test, minimal change, and verified refactor.
kind: builtin
platform_key: builtin.hermes.tdd
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read, workspace.write]}
steps: [{order: 1, name: Red, description: Write a focused failing test.}, {order: 2, name: Green, description: Make the smallest implementation pass.}, {order: 3, name: Refactor, description: Improve structure while tests stay green.}]
---
Write the smallest test that demonstrates the requested behavior, run it to confirm it fails for the intended reason, implement the smallest change, then run focused and relevant regression tests before refactoring.
