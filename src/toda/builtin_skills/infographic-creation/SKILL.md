---
name: infographic-creation
display_name: Infographic Creation
description: Turn structured information into polished SVG and PNG cards, timelines, comparisons, checklists, hierarchies, and narrative summaries using AntV Infographic.
version: 1.0.0
license: MIT
origin:
  project: AntV Infographic
  adapted_from: https://github.com/antvis/Infographic/tree/main/skills/infographic-creator
kind: builtin
platform_key: builtin.toda.infographic_creation
activation: {mode: optional}
required_capabilities: {all_of: [infographic.create]}
warnings:
  - Use chart_create for precise multi-series quantitative analysis and diagram_render for technical node-edge diagrams; an infographic is for compact visual storytelling.
  - Never treat a successful render as completion; read both artifacts and verify the visible labels, values, order, and language before delivery.
  - Send the PNG artifact to Slack and retain SVG as the editable source; do not expect Slack to render animated or interactive SVG consistently.
steps:
  - order: 1
    name: Structure
    description: Extract the smallest truthful information structure and select the matching template family.
  - order: 2
    name: Render
    description: Generate concise AntV DSL and call infographic_create for scoped SVG and PNG artifacts.
  - order: 3
    name: Verify and deliver
    description: Read back the artifacts, compare their content with the source evidence, and deliver the PNG through the authorized artifact-sharing tool.
---
# Infographic creation

Use `infographic_create` for a polished status card, completed-work summary,
timeline, process, comparison, hierarchy, or short narrative data story. Keep
ordinary prose as prose. Use `chart_create` when accurate axes and multiple
numeric series are the point, and `diagram_render` when relationships between
technical nodes are the point.

## Compose the DSL

Begin with `infographic <template-name>`, then add one `data` block and an
optional `theme` block. Use two-space indentation, `key value` pairs, and `-`
for array items. Keep visible text in the user's language.

Choose the template family from the information structure:

- `list-*` for parallel facts, status rows, or completed-work summaries.
- `sequence-*` for ordered stages, timelines, funnels, and roadmaps.
- `compare-*` for two-sided, SWOT, or quadrant comparisons.
- `hierarchy-*` for organizations, taxonomies, and mind maps.
- `relation-*` for a light relationship story; use `diagram_render` for a
  rigorous technical graph.
- `chart-*` only for a compact single-series story; use `chart_create` for
  analytic charts.

Use exactly one primary data field matching the template: `lists`,
`sequences`, `compares`, `root`, `nodes`, or `values`. Add a short semantic
`icon` phrase to list, sequence, comparison, and hierarchy items when it makes
the result easier to scan. Keep numbers as numeric `value` fields and put
units in labels or descriptions.

Example:

```infographic
infographic list-column-done-list
data
  title Onboarding summary
  lists
    - label Accounts provisioned
      desc Slack, Google, and GitHub
      icon shield check
    - label 12 onboarding tasks created
      icon checklist
    - label 3 intro meetings booked
      icon calendar
    - label Laptop ordered
      icon laptop
theme
  palette #6847ff #22c55e #171526
```

Do not include HTML, URLs, scripts, event handlers, or remote assets in the
DSL. The renderer is local and rejects them. Prefer a short title, no more
than six primary items for a Slack card, and descriptions that fit on one or
two lines.

## Verify the result

Call `infographic_create` with `format: both`. Confirm that it returns a real
`image.svg` source and `image.png` preview. Read both artifact records and
check the expected title, every label, every important value, order, and
source scope. If a label is missing or clipped, simplify the wording or choose
a better template and render again. Share the PNG artifact with
`slack_send_artifact`; keep the SVG artifact for later editing or embedding.
