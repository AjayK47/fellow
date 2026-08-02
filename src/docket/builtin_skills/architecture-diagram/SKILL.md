---
name: architecture-diagram
display_name: Architecture Diagram
description: Turn a system description into an accurate architecture diagram.
kind: builtin
platform_key: builtin.hermes.architecture_diagram
activation: {mode: optional}
required_capabilities: {}
steps: [{order: 1, name: Scope, description: Identify components and boundaries.}, {order: 2, name: Model, description: Show data and control flow.}, {order: 3, name: Verify, description: Check the diagram against the source.}]
---
Identify actors, components, trust boundaries, data stores, and flows before drawing. Label assumptions and keep the diagram at the requested level of detail.
