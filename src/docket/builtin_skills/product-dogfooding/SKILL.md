---
name: product-dogfooding
display_name: Product Dogfooding
description: Test a product through realistic user journeys and record actionable failures.
kind: builtin
platform_key: builtin.hermes.product_dogfooding
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
steps: [{order: 1, name: Journey, description: Define a realistic user path.}, {order: 2, name: Exercise, description: Use the product as a user would.}, {order: 3, name: Report, description: Record evidence and severity.}]
---
Use realistic inputs and state transitions. Record exact steps, expected versus observed behavior, logs or screenshots, severity, and a reproducible follow-up rather than vague impressions.
