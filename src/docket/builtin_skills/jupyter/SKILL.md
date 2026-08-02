---
name: jupyter
display_name: Jupyter Notebook
description: Analyze data in a reproducible notebook with explicit inputs and outputs.
kind: builtin
platform_key: builtin.hermes.jupyter
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read, workspace.write]}
steps: [{order: 1, name: Inspect, description: Understand notebook cells and data.}, {order: 2, name: Execute, description: Run bounded deterministic analysis.}, {order: 3, name: Verify, description: Preserve outputs and provenance.}]
---
Inspect notebook metadata and inputs, run only bounded code in the configured environment, preserve cell order and provenance, and report generated outputs and limitations.
