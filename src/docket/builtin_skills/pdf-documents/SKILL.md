---
name: pdf-documents
display_name: PDF and Document Handling
description: Read, extract, summarize, and create document artifacts while preserving source fidelity.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.pdf_documents
activation:
  mode: optional
required_capabilities:
  any_of: [documents.read, documents.write]
warnings:
  - Inspect the source before transforming it.
  - Report layout, OCR, and extraction limitations.
steps:
  - order: 1
    name: Inspect
    description: Determine file type, pages, structure, and whether OCR is needed.
  - order: 2
    name: Extract or transform
    description: Use the appropriate document tool and preserve citations.
  - order: 3
    name: Verify artifact
    description: Render or inspect the result and report any fidelity limitations.
---
Inspect the source file before transforming it. Choose the appropriate PDF or
office-document tool, preserve source meaning and citations, and do not claim
layout fidelity without inspecting the rendered artifact. Report the output path,
what was extracted or changed, and any OCR, formatting, or missing-content limits.
