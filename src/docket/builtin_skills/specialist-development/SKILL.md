---
name: specialist-development
display_name: Specialist Worker Development
description: Delegate a bounded read-only investigation with a precise artifact contract.
kind: builtin
platform_key: builtin.hermes.specialist_development
activation: {mode: optional}
required_capabilities: {}
steps: [{order: 1, name: Bound, description: Define scope and allowed evidence.}, {order: 2, name: Execute, description: Run independent work in parallel where safe.}, {order: 3, name: Synthesize, description: Merge artifacts and conflicts.}]
---
Give each specialist one bounded question, explicit inputs, allowed tools, output schema, and stop condition. Parallelize independent read-only work, then reconcile artifacts centrally without letting a worker bypass permissions.
