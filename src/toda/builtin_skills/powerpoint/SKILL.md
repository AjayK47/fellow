---
name: powerpoint
display_name: PowerPoint
description: Create and read slide decks with Toda's artifact-based pptx tools, keeping one idea per slide.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.powerpoint
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Outline, description: Define the story and audience.}, {order: 2, name: Build, description: Use consistent slide structure.}, {order: 3, name: Render, description: Inspect slides for clipping and hierarchy.}]
warnings:
  - pptx_create only accepts a title and a single text block per slide -- there is no support for images, charts, custom layouts, or design styling in this tool.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
---
Toda's PowerPoint support is artifact-based: `pptx_read`, `pptx_create`,
`pptx_edit`, and `pptx_render` all operate on an `artifact_id`, never a
filesystem path. The creation tool is intentionally minimal -- a title and a
body of text per slide, using a fixed "title and content" layout. It is not
a design tool: there is no palette, template, image, chart, or icon support
in this pipeline. Set expectations with the user accordingly rather than
describing capabilities (custom color themes, image placement, chart
embedding) this tool doesn't have.

## When to use this skill

Use `pptx_create` only when the user explicitly asks for slides, a deck, a
presentation, or PowerPoint. If the ask is really a written report or
summary and the user hasn't asked for slides, prefer `pdf_create` --
turning a report into a deck just because it has sections is the wrong
default. Use `pptx_read` to inspect an existing deck's content before
answering questions about it or before editing it.

## Reading (`pptx_read`)

Returns a list of slides in order; each entry has the slide number and a
list of the text found in that slide's shapes (title and body placeholders,
capped at 100 text items per slide). There is no access to speaker notes,
image content, chart data, animations, or slide layout/theme information --
if the user asks what's on a slide visually (a chart, an image, a diagram),
say that this tool only surfaces text and you cannot see non-text content
without a render step.

## Creating (`pptx_create`)

Takes either a `slides` array of `{title, text}` objects (one object per
slide) or, if `slides` is omitted, a single `{title, text}` slide built from
top-level `title`/`text` arguments. Every slide uses the same built-in
"title and content" layout -- there is no way to vary layout per slide, add
a title-only or section-divider slide, or place multiple text blocks side
by side.

- **One clear idea per slide.** Since there's no sub-layout control, keep
  each slide's `text` to a single coherent block (a short list of points as
  separate lines) rather than trying to cram two unrelated sections onto
  one slide.
- **Plan the story first.** Before calling the tool, decide the sequence of
  slides (title, agenda, 2-4 content slides, close) and the one point each
  should make -- the tool won't impose structure for you.
- **No images or charts.** If the user's request depends on a chart, photo,
  icon, or diagram, say plainly that this tool only produces plain
  text-and-title slides, and offer the closest available alternative (e.g.
  describing the data in words, or producing the chart as a separate
  artifact if another tool supports it) rather than pretending to embed one.
- **Text only, no rich formatting.** There is no bold/italic, bullet
  styling, or font control -- lines break the way you write them in `text`;
  simulate a bullet with a leading dash or number if that helps readability.
- Keep slide titles short (they render in the deck's title placeholder) and
  put the substance in `text`.

## Editing and rendering

`pptx_edit` and `pptx_render` are declared operations in this tool family;
if a call comes back `setup_needed`, that means the optional renderer for
this deployment isn't configured -- report that plainly rather than
claiming you visually checked a deck you couldn't render. When rendering is
available, use it to catch the most common real defects: text spilling
past its placeholder, a body block clearly too dense for the slide, or a
title that's actually the wrong length for a title placeholder. This tool
family has no design polish to check for (color, icons, motif) because the
creation tool doesn't produce any -- verification here is about content
placement and legibility, not visual design.

## Verifying and delivering

After creating a deck, a quick `pptx_read` of the result confirms every
slide's title and text landed as intended and in the right order -- cheap
insurance against a `slides` array built in the wrong sequence.

Every `pptx_create` result includes a `download_url` field only when a
public URL is configured for this deployment. Share that exact string
verbatim when present. If it's absent, tell the user plainly that the deck
was created but no shareable link is available right now -- never
fabricate one.

## Related skills

`docx` (Word documents), `xlsx` (spreadsheets), `pdf-documents` (the
default format for a finished report when slides weren't requested).
