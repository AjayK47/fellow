---
name: pdf-documents
display_name: PDF and Document Handling
description: Create a downloadable PDF report, brief, summary, or document -- and read, extract, merge, split, or edit an existing PDF -- with Toda's real-layout, artifact-based PDF tools.
version: "2.0.0"
license: Apache-2.0
origin:
  project: toda
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
  - No external resources in pdf_create's html -- no font links, no CDN
    scripts, no remote image URLs. Everything must be self-contained
    inline CSS, inline SVG, or a data URI, exactly like html_create.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
steps:
  - order: 1
    name: Inspect
    description: Determine file type, pages, structure, and whether OCR is needed (for an existing PDF you're transforming).
  - order: 2
    name: Design
    description: For a new PDF, design each page as real HTML/CSS first -- same taste and process as html_create (see the visual-design skill) -- before touching pdf_create.
  - order: 3
    name: Render and verify
    description: Convert the designed HTML to a real paginated PDF via pdf_create, then render or inspect the result and report any fidelity limitations.
---
Toda's PDF support is artifact-based: `pdf_inspect`, `pdf_extract`,
`pdf_render`, `pdf_create`, `pdf_edit`, `pdf_merge`, `pdf_split`, and
`pdf_fill_form` all take (or, for `pdf_create`, produce) an `artifact_id` --
never a filesystem path. This is the complete PDF tool surface; there is no
shell, no external PDF library call, and no direct file access outside these
named operations.

## Creating a PDF: design the page, then render it (`pdf_create`)

`pdf_create` renders through **WeasyPrint**, a genuine HTML/CSS layout
engine (Pango/cairo) -- not a plain-text canvas. It produces real pagination,
typography, tables, images, and print-specific layout via CSS. That changes
the actual workflow for a new PDF into two separate steps, and skipping the
first one is the single most common way a PDF ends up looking generic:

1. **Design the page(s) as HTML/CSS first**, using exactly the same taste
   and process the `visual-design` skill already describes for
   `html_create`: calibrate utilitarian vs. editorial treatment, plan a
   named color/type token system, build real typographic hierarchy. A PDF
   report deserves the same design discipline as a page someone will look
   at in a browser -- the only thing that changes is how it's delivered.
2. **Convert with `pdf_create(title, html)`** once the HTML is designed.
   Pass the full HTML document as the `html` argument -- WeasyPrint paginates
   it for you using the CSS below.

Do not skip straight to a bare `text` argument for anything beyond a
one-line note. `text` alone still works (wrapped in a clean, readable
default template) so a trivial ask isn't blocked, but any request with
real content -- a report, a summary, a proposal -- deserves a properly
designed `html` page, exactly the effort you'd put into `html_create`.

### Controlling pagination and page setup with CSS

WeasyPrint implements CSS Paged Media, so page structure is real CSS, not a
workaround:

```css
@page {
  size: letter;           /* or "A4", "letter landscape", etc. */
  margin: 2.5cm 2cm;
}

/* Force a new page before/after an element */
.section { break-before: page; }   /* or break-after: page */

/* Avoid an awkward split -- keep a block on one page */
.card { break-inside: avoid; }

/* A distinct first page (e.g. a cover page with no header) */
@page :first { margin-top: 4cm; }
```

Use `break-before`/`break-after: page` between logical "pages" of a
multi-section report the same way you'd think about slide boundaries in a
deck -- one call renders the whole document, so express every page break
in the HTML/CSS itself rather than making multiple `pdf_create` calls and
merging them.

Running headers/footers, page numbers, tables, and images all render
exactly as they would in `html_create` (inline SVG for charts/diagrams;
`<table>` for tabular data; data-URI images) -- there is no separate
mechanism for print output; it's the same design surface, just paginated.

### When to still reach for `html_create` instead

`html_create` remains the right call when the deliverable is meant to be
*viewed* in the browser from its `download_url` rather than downloaded as
a file -- the user didn't ask for a specific file format and visual
richness is the point, but a real downloadable document isn't. Once the
user wants an actual PDF (explicitly, or because it needs to be
downloaded/shared/printed/archived as a file), use `pdf_create` with the
same designed HTML instead.

## Inspect before you transform an existing PDF

Before extracting, merging, splitting, or editing an existing PDF, call
`pdf_inspect` first. It returns page count, available metadata, and whether
the file is encrypted. This single call tells you two things that change
your plan: whether the requested page range is even valid, and whether the
document is encrypted (in which case downstream reads may fail until it's
handled) -- don't skip straight to `pdf_extract` on a document you haven't
inspected and then report a confusing failure as if it were unexpected.

## Extracting text (`pdf_extract`)

Takes an `artifact_id` and an optional `pages` array (1-indexed); omitted,
it extracts every page. Returns each requested page's number and its raw
text. This is text-layer extraction only -- a scanned page with no
embedded text layer returns an empty string for that page, not an error.
If a page's text comes back empty (or clearly truncated relative to what
you'd expect from a page of that length), don't guess at the content: say
the page appears to be a scanned image and route it to the `ocr-documents`
skill's `ocr_extract` tool instead. Preserve the page numbers you cite when
quoting extracted text back to the user, so they can locate it in the
source.

## Rendering a page (`pdf_render`)

Renders one page (1-indexed, default page 1) to a PNG image artifact at a
fixed 1.5x scale. Use this whenever you need to actually look at a page --
to verify a fill, a merge, a split, a new pdf_create layout, or to answer a
question about layout, a table, or an image that `pdf_extract`'s plain
text can't capture. Each call renders exactly one page and returns a new
image artifact, so render each page you need to check individually rather
than assuming one render covers the whole document.

## Merging and splitting

`pdf_merge` takes an `artifact_ids` array and concatenates their pages in
the order given into one new PDF artifact -- order matters, so confirm the
sequence the user wants before calling it. `pdf_split` takes one
`artifact_id` and an optional `pages` array (default: every page) and
returns one new PDF artifact per requested page. Both preserve the original
page content exactly; neither renumbers, re-paginates, or re-flows anything.

## Form filling (`pdf_fill_form`)

Declared for filling PDF form fields on an existing artifact. If a call
returns a `setup_needed` result, its optional runtime isn't configured in
this deployment -- report that plainly rather than claiming the form was
filled. When it is available, verify the fill by rendering the affected
page with `pdf_render` and checking the values landed in the right fields
before telling the user it's done.

## Verifying and delivering

Don't claim layout or extraction fidelity you haven't actually checked.
After any create, merge, split, or edit, either re-inspect the artifact
(`pdf_inspect` for page count) or render the relevant page and look at it
before reporting success -- for a newly designed `pdf_create` page, render
every page at least once to confirm the layout, page breaks, and any
tables/charts actually landed the way the HTML intended. State any real
limitation plainly: a scanned page with no text layer, a merge whose page
order might not match what the user expected.

Every `pdf_create`/`pdf_merge`/`pdf_split`/`pdf_edit` result includes a
`download_url` field only when a public URL is configured for this
deployment. Share that exact string verbatim when present; if it's absent,
say plainly that the file was created but no shareable link is available
right now -- never construct or guess one.

## Related skills

`visual-design` (the design taste and token-system process to use *before*
calling `pdf_create` with `html`), `ocr-documents` (text extraction from
scanned pages), `nano-pdf` (small, precise edits to an existing PDF's
content), `docx` (Word documents), `xlsx` (spreadsheets), `powerpoint`
(slide decks).
