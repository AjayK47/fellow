---
name: humanizer
display_name: Humanize Writing
description: Make generated writing direct, natural, specific, and appropriate for its audience.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.humanizer
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Do not remove necessary technical precision.
steps:
  - order: 1
    name: Remove filler
    description: Cut generic openings, repetition, and unnecessary hedging.
  - order: 2
    name: Use concrete language
    description: Prefer active voice, specific verbs, and useful examples.
  - order: 3
    name: Preserve intent
    description: Keep facts, constraints, and requested tone intact.
---
Edit for a real reader. Remove filler, generic openings, repetition, and empty
hedging. Prefer active voice, concrete verbs, and specific examples. Preserve
technical precision, facts, constraints, and the requested tone.
