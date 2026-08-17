---
name: architecture-diagram
display_name: Architecture Diagram
description: Turn a system description into an accurate architecture diagram.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.architecture_diagram
activation: {mode: optional}
required_capabilities: {any_of: [diagram.render, documents.write]}
warnings:
  - Draw arrows before the component boxes in the SVG so boxes render on top
    and arrows never overlap illegible text.
  - Place the legend below every boundary box, never overlapping one --
    compute the lowest boundary's y-coordinate first.
steps:
  - order: 1
    name: Scope
    description: Identify components, trust boundaries, and data flow from the source description.
  - order: 2
    name: Model
    description: Lay out components and connections using the dark tech design system below.
  - order: 3
    name: Verify
    description: Check the diagram against the source description before delivering it.
---
Use this for software system architecture, cloud infrastructure, service
topology, or a deployment diagram -- anything that fits a dark, technical
aesthetic. For physics/chemistry/biology/physical-object diagrams or a
lighter educational style, prefer the concept-diagrams skill instead. Use
`diagram_render` for the normal case: components and directed relationships
are exactly what Graphviz lays out reliably. Use inline SVG inside
`html_create` only when Graphviz reports `setup_needed`, or when the visual is
genuinely free-form and cannot be represented as nodes, clusters, and edges.

## Scope before drawing

From the system description, identify: the components (frontend, backend,
database, queue, external services), the trust or network boundaries
(VPC, security group, region), and the direction of each data or control
flow between them. If the description is ambiguous about a boundary or a
flow direction, say what you assumed rather than silently picking one.

## Visual language

- **Background**: near-black (`#020617`), with an optional faint 40px grid
  pattern for texture -- not a decorative gradient.
- **Typography**: a monospace font, small sizes (roughly 12px for component
  names, 9px for sublabels) -- this is what gives it a technical, not
  corporate, feel.
- **Color encodes component type**, consistently, e.g.:

  | Component type | Fill | Stroke |
  |---|---|---|
  | Frontend | translucent dark teal | cyan |
  | Backend | translucent dark green | emerald |
  | Database | translucent dark purple | violet |
  | Cloud/managed service | translucent dark amber | amber |
  | Security boundary | translucent dark rose | rose |
  | External/third-party | translucent slate | slate |

  Pick one mapping and hold it constant across the whole diagram -- don't
  let the same component type appear in two different colors.

## Drawing components and connections

For `diagram_render`, represent trust or network boundaries as DOT
`subgraph cluster_*` groups, use `rankdir=LR` for ordinary request/data-flow
systems, and label edges only when the protocol or event name adds real
information. Do not add URL, image, stylesheet, or filesystem-backed
attributes; the renderer rejects active and host-referencing content.

When the inline-SVG fallback is necessary:

- Components are rounded rectangles (`rx="6"`) with a thin stroke
  (~1.5px). Because the fills are translucent, draw an opaque dark
  background rectangle first, then the styled translucent rectangle on top,
  so arrows passing behind a box don't visually bleed through it.
- Draw connector lines/paths early in the SVG (right after the background
  grid), before any component box -- this makes boxes render on top of
  arrows instead of arrows crossing over box text.
- Give every connector `fill="none"` and an arrowhead marker; use a dashed
  stroke in the security/rose color specifically for a security- or
  auth-related flow, so it reads differently from ordinary data flow at a
  glance.
- Draw a security-group boundary as a small dashed rectangle and a larger
  region boundary as a bigger dashed rectangle with more rounded corners
  (`rx="12"`), so nesting is visually legible.

## Layout

Leave at least ~40px of vertical gap between components so connectors have
room to route cleanly, and place a message bus or queue in the gap between
the services it connects rather than overlapping either one. Compute the
lowest y-coordinate used by any boundary box and place the legend at least
20px below that -- a legend that overlaps a boundary is the most common
avoidable error in this style.

## Verify before delivering

Re-read the source description against the finished diagram: does every
named component appear, does every described connection exist with the
right direction, and does no arrow silently cross through an unrelated box?
State any simplification you made (e.g. "grouped the three worker replicas
into one box") rather than presenting an intentionally simplified diagram as
if it were exhaustive. A successful render call is not completion: retrieve
the returned artifact, confirm it is a real SVG, and compare its node and edge
labels against the source description before delivery.
