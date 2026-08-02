---
name: spike
display_name: Technical Spike
description: Answer one technical unknown with a time-boxed experiment.
kind: builtin
platform_key: builtin.hermes.spike
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
steps: [{order: 1, name: Hypothesis, description: Define the unknown and success signal.}, {order: 2, name: Experiment, description: Build the smallest probe.}, {order: 3, name: Record, description: Capture evidence and recommendation.}]
---
Time-box the investigation. Define one hypothesis and a measurable success signal, run the smallest safe experiment, record evidence, and finish with a decision or a clearly bounded follow-up.
