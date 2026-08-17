---
name: visual-design
display_name: Visual Design for HTML Artifacts
description: Design taste for html_create and pdf_create -- typography, color, and layout choices that avoid generic AI-generated look.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.visual_design
activation: {mode: optional}
required_capabilities: {all_of: [documents.write]}
warnings:
  - No external resources -- no font links, no CDN scripts, no remote image
    URLs. Everything must be self-contained inline CSS, inline SVG, or a
    data URI; there is no guarantee the rendering environment can reach
    the internet.
  - There is no Mermaid or client-side JS charting runtime in an HTML
    artifact. Use chart_create for standard charts, diagram_render for
    node-and-edge diagrams, and inline SVG only for free-form geometry.
  - Design for both light and dark viewing with CSS custom properties and
    a prefers-color-scheme override -- never invert a palette naively and
    assume it still has enough contrast.
steps:
  - order: 1
    name: Calibrate
    description: Read the request to decide utilitarian vs. editorial treatment before writing any markup.
  - order: 2
    name: Plan tokens
    description: Choose a small named color and type system grounded in the subject, before improvising styles inline.
  - order: 3
    name: Build and verify
    description: Build responsive, self-contained HTML/CSS/SVG and check both color-scheme themes before delivering it.
---
This skill is the design layer behind both `html_create` (a page viewed in
the browser from its `download_url`) and `pdf_create`'s `html` argument (the
same design surface, rendered to a real paginated PDF via WeasyPrint --
see the `pdf-documents` skill for the CSS Paged Media specifics: `@page`,
`break-before`/`break-after: page`, and so on). The system prompt already
tells you *when* to reach for `html_create`/`pdf_create` over `docx_create`
-- reach for one of these two when the user wants something visually rich;
which of the two depends on whether they want a downloadable/printable file
(`pdf_create`) or just something to look at in the browser (`html_create`).
This skill covers what to actually do once you've made that call: how to
make the resulting page look considered rather than like a template
default.

One difference when the target is `pdf_create` rather than `html_create`:
skip the light/dark dual-theme treatment below (a printed or downloaded PDF
has no `prefers-color-scheme` toggle to respond to) -- pick one considered
light theme and design for print instead.

## Calibrate treatment to the request first

Before writing a line of markup, decide what kind of artifact this is.

- **Utilitarian** -- a status report, a plan, an internal memo, a
  comparison table. This still deserves real typographic hierarchy,
  deliberate spacing, and a coherent palette, but not a hero section, a
  large display headline, or decorative flourishes. Restraint is the right
  call here, not a lesser effort.
- **Editorial** -- something the user will keep, present, or share outward:
  a one-pager, a proposal, a visual summary meant to impress. This earns a
  more distinctive visual identity -- a bolder type pairing, a more
  considered layout concept, maybe a single well-placed graphic moment.

Reread the actual request rather than defaulting to one mode. A "put
together a quick summary of this data" is utilitarian even if it involves a
chart; a "make me something I can share with the team" about the same data
is editorial. When genuinely unsure, treat it as utilitarian -- restrained
and legible fails safer than an over-designed page for a two-line memo.

## Plan a token system before styling anything

Improvising colors and spacing as you write markup is how pages end up
looking inconsistent. Instead, decide on paper (or in your own scratch
notes) before touching HTML:

- **4-6 named colors**: a background, a surface/card tone, a primary text
  color, a muted/secondary text color, and one or two accents. Write them
  as hex values you intend to reuse verbatim, not values you'll eyeball
  each time you need a border.
- **Two type roles**: a display/heading face and a body face. Both need to
  come from safe system-font stacks (for example a stack ending in
  `system-ui, -apple-system, Segoe UI, sans-serif`, or a monospace stack
  for technical content) -- there is no reliable way to load a custom
  webfont here, so don't write a `@font-face` rule that depends on a
  network fetch.
- **One sentence describing the layout concept**: what the eye sees first,
  how sections are ordered, whether it's a single column, a grid, or a
  report-with-sidebar shape. If you can't state this in one sentence,
  you don't have a plan yet.

Only after those three decisions exist should you start writing the HTML
and CSS -- build from the plan, not toward it.

## Avoid the tells of generic AI-generated design

A handful of looks show up so often in default LLM output that they read
as a giveaway rather than a deliberate choice. None of these are wrong in
isolation -- the tell is reaching for one by default instead of because the
subject actually called for it:

- Warm cream background paired with a serif display face and a
  terracotta or rust accent color.
- Near-black background with exactly one neon-green or vermilion accent
  and nothing else.
- A purple-to-blue gradient hero band at the top of the page.
- One generic sans (Inter or the default system sans) used for everything,
  with no second face for contrast.
- Emoji standing in for section markers or bullet points.
- Every block of content centered, including body text.
- The exact same `border-radius` value applied to literally every element,
  with no variation between, say, a card and a small tag.
- A thin accent-colored vertical bar down the left edge of every card,
  used as a default decoration rather than because it means something.

If a first draft in your head matches one of these, stop and ground the
palette and type choice in the actual subject instead: what would a
finance-team memo, a lab report, or a product retro actually look like,
rather than what a generic SaaS landing page looks like.

## Ground choices in the subject

The content should drive the palette and type, not the reverse. Financial
or operational content can lean toward a restrained, numeric-friendly
system (tabular figures, a muted palette, clear rules between sections).
Technical or scientific content can lean toward a monospace accent for
data and code, tighter tracking, cooler tones. A narrative or strategy
document can afford warmer tones and a stronger display face. Pick a
palette because it fits the report in front of you, not because it's a
safe default.

## Light and dark themes are both required

Toda's HTML artifacts render inline in whatever theme the viewer has
active, so a page styled for only one theme is half-finished. Define
tokens as CSS custom properties on `:root`, then override them inside
`@media (prefers-color-scheme: dark)`:

```
:root {
  --bg: #f7f5f1;
  --surface: #ffffff;
  --text: #1f2320;
  --muted: #5b6560;
  --accent: #2f6f5e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14181a;
    --surface: #1d2225;
    --text: #e7ebe8;
    --muted: #9aa5a0;
    --accent: #6fd3b8;
  }
}
```

Don't just invert lightness values and call it done -- re-check that body
text still has enough contrast against the dark background and that the
accent color hasn't gone muddy or illegible once the surface darkens.

## Diagrams and charts have to be real markup

There is no Mermaid or client-side chart runtime inside `html_create`.
Standard bar, line, pie, and scatter charts should be produced with
`chart_create`; node-and-edge architecture or flow diagrams should use
`diagram_render`. Retrieve the resulting SVG artifact and inline its actual
SVG text when it belongs inside a larger HTML or PDF composition. Use
hand-written inline SVG only when the visual is free-form or physical and the
native tools cannot represent it honestly. Never write a Mermaid div or name
a library and assume something downstream will render it.

## Structural honesty

Only add structure that reflects something real in the content. A
numbered "01 / 02 / 03" treatment implies a genuine sequence -- don't apply
it to a set of items that are actually parallel or unordered just because
numbering looks more designed. Same for badges, dividers, and step
indicators: each one should correspond to a real distinction the content
makes (a status, a category, a phase), not a decoration added after the
fact.

## Responsive basics and restraint on motion

- Wide content -- a table, a code block, a wide diagram -- scrolls inside
  its own container (`overflow-x: auto` on that element) rather than
  forcing the whole page wider than the viewport.
- Use relative units (`rem`, `%`, `ch`) over fixed pixel widths where the
  layout needs to flex, and give headings real breathing room instead of
  packing content edge-to-edge.
- A static, well-composed page beats an over-animated one. If you use
  motion at all, keep it to a single purposeful load-in rather than
  animating every element, and wrap it in
  `@media (prefers-reduced-motion: no-preference)` so a viewer who has
  asked for less motion gets the static version.

## Delivering the result

For a browser-viewed page, pass the finished, self-contained HTML to
`html_create` and present its `download_url` as a link to open and view,
exactly as the system prompt's document-tool guidance describes -- never as
a file to download, and never a constructed or guessed URL. For a
downloadable/printable PDF, pass the same HTML to `pdf_create`'s `html`
argument instead (adding `@page`/`break-after: page` rules per the
`pdf-documents` skill), and present that result's `download_url` as an
actual file to download. Before calling it done, mentally check the page
against both the light and dark override (browser target) or against a
single considered print theme (PDF target), confirm nothing depends on a
network fetch, and confirm any diagram is real SVG or canvas rather than a
placeholder for a library that isn't there -- then render at least one page
(`pdf_render`, for a PDF target) to verify the layout actually landed as
designed before reporting success.
