---
name: one-three-one
display_name: One-Three-One Updates
description: Structure status updates as one headline, three facts, and one ask.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.one_three_one
activation:
  mode: optional
required_capabilities: {}
steps:
  - order: 1
    name: Headline
    description: State the current status in one sentence.
  - order: 2
    name: Three facts
    description: Give the three most decision-relevant facts.
  - order: 3
    name: One ask
    description: State one clear decision or action needed.
---
For a status update, use one short headline, three decision-relevant facts, and
one explicit ask. If there is no ask, say that no action is needed. Do not pad
the update with unrelated history.
