---
name: decision-communication
display_name: Decision Communication
description: Communicate decisions with a direct recommendation, tradeoffs, and next steps.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.decision_communication
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Mark actions that require human approval.
steps:
  - order: 1
    name: State the decision
    description: Lead with the recommended option.
  - order: 2
    name: Explain tradeoffs
    description: Compare only materially different alternatives.
  - order: 3
    name: Give next steps
    description: Assign concrete, reversible actions and approvals.
---
Lead with one clear recommendation. Explain the material tradeoff and the
evidence behind it. Mention alternatives only when they change the decision.
End with concrete next steps, owners where known, and any action that needs
human approval.
