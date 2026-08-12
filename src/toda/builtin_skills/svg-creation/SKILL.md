---
name: svg-creation
display_name: SVG Creation
description: Create safe, accessible custom SVG illustrations and restrained animations when templates, charts, and automatic graph layouts cannot express the required geometry.
version: 1.0.0
license: MIT
origin:
  project: supermemoryai skills
  adapted_from: https://github.com/supermemoryai/skills/blob/main/svg-animations/SKILL.md
kind: builtin
platform_key: builtin.toda.svg_creation
activation: {mode: optional}
required_capabilities: {all_of: [documents.write]}
warnings:
  - Prefer infographic_create, chart_create, or diagram_render when their structured layouts fit; handcrafted coordinates are the fallback, not the default.
  - Never include scripts, event handlers, external links, remote images, host paths, or active embedded content in an SVG artifact.
  - Animation is for HTML delivery only; provide a static PNG or SVG fallback for Slack and respect reduced-motion preferences.
steps:
  - order: 1
    name: Decide
    description: Confirm that the visual depends on custom spatial geometry or animation and cannot be represented by a structured renderer.
  - order: 2
    name: Draw
    description: Create a responsive, accessible inline SVG inside an HTML artifact using a small reusable visual system.
  - order: 3
    name: Verify
    description: Render the artifact, inspect the actual pixels and labels, and provide a static delivery fallback where required.
---
# Safe SVG creation

Use handcrafted SVG only for custom illustrations, annotated spatial
layouts, icons, animated logos, path drawing, or motion that a structured
renderer cannot express. Put the SVG inline inside `html_create`; do not ask
the execution tool to run arbitrary drawing code.

Define a `viewBox` and let the container size the image. Group related shapes
with `<g>`, put reusable gradients, masks, clip paths, and markers in `<defs>`,
and use two or three colors with consistent meaning. Add `role="img"`, a
`<title>`, and a `<desc>`. Give text explicit font size, weight, and contrast.

Prefer transforms and opacity for CSS animation. Use path dash offset for a
draw-on effect and ensure morphing paths have matching commands. Include a
`prefers-reduced-motion` rule that removes animation. Avoid expensive filters
and continuous path-shape animation on complex graphics.

All content must be self-contained. Do not use JavaScript, event attributes,
external resource references, remote fonts, embedded documents, or filesystem
paths. For Slack, deliver a static rendered preview; keep animation in the HTML
artifact only.

After creation, render the HTML, inspect the visible result, confirm that text
is not clipped and the intended first/static frame is meaningful, and revise
before delivery. A valid SVG document is not proof of a usable visual.
