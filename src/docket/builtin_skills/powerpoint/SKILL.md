---
name: powerpoint
display_name: PowerPoint
description: Create and edit slide decks with coherent structure and visual verification.
kind: builtin
platform_key: builtin.hermes.powerpoint
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Outline, description: Define the story and audience.}, {order: 2, name: Build, description: Use consistent slide structure.}, {order: 3, name: Render, description: Inspect slides for clipping and hierarchy.}]
---
Start from the story and audience, keep one clear idea per slide, preserve source assets, and render the deck to catch clipping, unreadable text, and inconsistent spacing.
