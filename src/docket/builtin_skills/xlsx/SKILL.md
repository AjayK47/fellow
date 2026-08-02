---
name: xlsx
display_name: XLSX Spreadsheets
description: Analyze and create spreadsheets with explicit formulas and verification.
kind: builtin
platform_key: builtin.hermes.xlsx
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Inspect, description: Identify sheets, ranges, and formulas.}, {order: 2, name: Transform, description: Preserve types and formulas.}, {order: 3, name: Verify, description: Reopen and check key cells.}]
---
Inspect workbook structure before editing. Preserve formulas and types, avoid accidental broad range changes, and reopen or render the result to verify key values and layout.
