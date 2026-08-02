---
name: ocr-documents
display_name: OCR and Documents
description: Extract text from scanned documents and preserve uncertainty.
kind: builtin
platform_key: builtin.hermes.ocr_documents
activation: {mode: optional}
required_capabilities: {all_of: [documents.read]}
steps: [{order: 1, name: Detect, description: Determine whether OCR is needed.}, {order: 2, name: Extract, description: Run OCR and retain page references.}, {order: 3, name: Review, description: Flag low-confidence text.}]
---
Detect scanned pages, run OCR with page references, preserve the source artifact, and mark uncertain or ambiguous text rather than silently correcting it.
