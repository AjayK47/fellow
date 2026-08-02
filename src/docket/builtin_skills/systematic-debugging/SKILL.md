---
name: systematic-debugging
display_name: Investigate Before Fixing
description: Investigate issues from evidence before recommending or applying a fix.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.systematic_debugging
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Separate observed facts from assumptions.
steps:
  - order: 1
    name: Establish the symptom
    description: Restate the failure and its scope in testable terms.
  - order: 2
    name: Gather evidence
    description: Inspect logs, state, history, and reproducible examples.
  - order: 3
    name: Verify the fix
    description: Test the smallest change and state remaining uncertainty.
---
Investigate before changing anything. Separate facts from hypotheses, identify
the smallest reproducible symptom, inspect relevant logs and history, and trace
the failure to a root cause. Recommend the smallest verified fix, explain how it
was tested, and state confidence and unknowns explicitly.
