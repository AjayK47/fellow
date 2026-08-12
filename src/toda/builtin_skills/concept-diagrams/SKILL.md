---
name: concept-diagrams
display_name: Concept Diagrams
description: Explain an idea with a compact, audience-appropriate visual model.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.concept_diagrams
activation: {mode: optional}
required_capabilities: {any_of: [diagram.render, documents.write]}
warnings:
  - Every <text> element needs an explicit font-size/weight; don't leave text
    unstyled and unreadable against the diagram background.
  - Keep the diagram to 2-3 colors with a real meaning each -- don't cycle
    through a rainbow palette for decoration.
steps:
  - order: 1
    name: Frame
    description: Name the central concept and pick the smallest diagram type that explains it.
  - order: 2
    name: Connect
    description: Show only meaningful relationships, laid out with consistent spacing.
  - order: 3
    name: Explain
    description: Add a short legend and verify the SVG renders sensibly before delivering it.
---
Use this for an educational or conceptual visual -- a process, a physical
object, a system's structure, or a lifecycle -- when a diagram would
communicate faster than a paragraph. Use `diagram_render` when the concept is
naturally nodes and relationships: a flowchart, lifecycle, dependency graph,
hierarchy, or hub-spoke model. Use inline SVG inside `html_create` for physical
cross-sections, spatial layouts, annotated objects, and other visuals whose
geometry carries meaning that automatic graph layout would destroy. Use the
data-visualization skill—not this one—for quantitative charts.

## Best fit vs. look elsewhere

This skill suits physics/chemistry/biology setups, physical objects and
cross-sections, floor plans, lifecycle/journey diagrams, hub-spoke system
integrations, and general educational visuals. For software or cloud
infrastructure with a dark, technical aesthetic, prefer the
architecture-diagram skill instead -- it's a better fit for that subject.
If neither matches well, this skill's flat educational style is still a
reasonable general fallback.

## Design system (flat, minimal, consistent)

- **Flat**: no gradients, drop shadows, blur, or glow. Solid fills and thin
  strokes only.
- **Minimal**: show only what's necessary to understand the relationship.
  No decorative icons inside boxes, no ornamental flourishes.
- **Color means category, not decoration.** Pick 2-3 colors, assign each to
  one category of node (e.g. inputs, processing, outputs), and reuse it
  consistently. Use a neutral gray for structural/start/end nodes. Never
  cycle through many colors just to look varied.
- **Two text sizes only**: a ~14px/medium-weight size for titles and a
  ~12px/regular size for subtitles or annotations. Sentence case always,
  never Title Case or ALL CAPS.
- **Thin strokes**: roughly 0.5-1px on node borders; rounded corners
  (`rx="6"` to `rx="12"`) read as "diagram," sharp corners read as "form."
- **Consistent spacing**: pick one grid unit (e.g. 20px) and space every box
  and gap as a multiple of it, so the layout doesn't look improvised.

## Building the SVG

For graph-shaped concepts, write compact Graphviz DOT and use
`diagram_render`; do not hand-compute node positions or edge routes. Retrieve
and check the returned SVG artifact before delivery. If Graphviz is unavailable
or the subject needs free-form geometry, use the inline-SVG guidance below.

Include an arrow marker once and reuse it for every connector:

```
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
          markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" stroke="currentColor" stroke-width="1.5"/>
  </marker>
</defs>
```

Every connector line or path needs `fill="none"` (SVG defaults to a filled
black shape otherwise) and `marker-end="url(#arrow)"` when direction matters
-- omit the arrowhead entirely for a relationship that isn't directional.
Size the `viewBox` to the actual content plus a small margin rather than an
arbitrary large canvas, and verify no arrow crosses through an unrelated box
before calling the diagram done.

## Choosing the diagram type

Match the layout to the subject rather than defaulting to a generic
flowchart: a flowchart for a linear process with branches, a containment
diagram (nested boxes) for structural/nesting relationships, a hub-spoke
layout for a central thing connecting to several others, a left-to-right
data-flow for a pipeline, and a physical/cross-section rendering (paths and
polygons matching the real shape) for a physical object.

## Delivering it

Wrap the SVG in a minimal self-contained HTML page (a title, the SVG, and
enough inline CSS to set a background and support both light and dark
viewing) and pass it to `html_create`. Present the result as a link to open
and view, not a file to download, per the document-tool guidance in the
system prompt. Add a one-line legend under the diagram naming what each
color represents so it's understandable without re-reading the whole
explanation.
