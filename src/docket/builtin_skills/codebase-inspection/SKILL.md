---
name: codebase-inspection
display_name: Codebase Inspection
description: Build an evidence-backed map of an unfamiliar repository before editing it.
kind: builtin
platform_key: builtin.hermes.codebase_inspection
activation: {mode: optional}
required_capabilities: {all_of: [workspace.list, workspace.read]}
steps: [{order: 1, name: Inventory, description: Locate entry points and boundaries.}, {order: 2, name: Trace, description: Follow the relevant execution path.}, {order: 3, name: Report, description: State findings and unknowns.}]
---
Inventory the repository, identify configuration and entry points, trace the relevant path end to end, and cite the files that support each conclusion. Preserve unrelated user changes.
