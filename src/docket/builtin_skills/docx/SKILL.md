---
name: docx
display_name: DOCX Documents
description: Read and create Word documents with verified structure and rendering.
kind: builtin
platform_key: builtin.hermes.docx
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Inspect, description: Read structure and styles.}, {order: 2, name: Edit, description: Apply the requested content change.}, {order: 3, name: Render, description: Verify the final document visually.}]
---
Preserve headings, lists, tables, links, and styles. Render generated documents before delivery and report unsupported layout features.
