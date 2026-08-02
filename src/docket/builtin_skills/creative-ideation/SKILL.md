---
name: creative-ideation
display_name: Creative Ideation
description: Generate, compare, and shape novel ideas against explicit constraints.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.creative_ideation
activation:
  mode: optional
required_capabilities: {}
steps:
  - order: 1
    name: Frame constraints
    description: Restate audience, goal, resources, and non-negotiables.
  - order: 2
    name: Diverge
    description: Produce several genuinely different directions.
  - order: 3
    name: Converge
    description: Compare options and recommend the strongest next experiment.
---
First frame the audience, goal, resources, and non-negotiables. Generate several
meaningfully different directions before judging them. Then compare feasibility,
impact, and risk, recommend one option, and propose the smallest experiment that
can validate it.
