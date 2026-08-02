---
name: watchers
display_name: General Watchers
description: Run a durable watch for meaningful external or company changes.
kind: builtin
platform_key: builtin.hermes.watchers
activation: {mode: optional}
required_capabilities: {}
steps: [{order: 1, name: Baseline, description: Record the last observed state.}, {order: 2, name: Detect, description: Compare new evidence with the baseline.}, {order: 3, name: Notify, description: Deliver proportional, deduplicated updates.}]
---
Persist a baseline and compare new evidence against it. Avoid repeated notifications, distinguish changed facts from noise, and respect the destination's delivery policy.
