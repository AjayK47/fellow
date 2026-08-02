---
name: nano-pdf
display_name: Nano PDF Editing
description: Make small, targeted PDF edits while preserving the rest of the artifact.
kind: builtin
platform_key: builtin.hermes.nano_pdf
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Inspect, description: Render and locate the target.}, {order: 2, name: Edit, description: Apply only the requested local change.}, {order: 3, name: Verify, description: Render the result and compare.}]
---
Inspect the page visually before editing. Make the smallest local change, preserve unrelated content, render the result, and report any font, layout, or metadata changes.
