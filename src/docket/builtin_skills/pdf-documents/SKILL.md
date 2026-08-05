---
name: pdf-documents
display_name: PDF and Document Handling
description: Read, extract, merge, split, and create PDF artifacts with Docket's artifact-based PDF tools.
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
  - pdf_create builds plain text into a PDF (reportlab) -- it is not a layout engine; it cannot reproduce rich formatting, tables, or images.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
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
Docket's PDF support is artifact-based: `pdf_inspect`, `pdf_extract`,
`pdf_render`, `pdf_create`, `pdf_edit`, `pdf_merge`, `pdf_split`, and
`pdf_fill_form` all take (or, for `pdf_create`, produce) an `artifact_id` --
never a filesystem path. This is the complete PDF tool surface; there is no
shell, no external PDF library call, and no direct file access outside these
named operations.

## Inspect before you transform

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
to verify a fill, a merge, a split, or to answer a question about layout,
a table, or an image that `pdf_extract`'s plain text can't capture. Each
call renders exactly one page and returns a new image artifact, so render
each page you need to check individually rather than assuming one render
covers the whole document.

## Creating a PDF (`pdf_create`)

Takes a `title` and `text`, and builds a plain-text PDF via reportlab --
each line of `text` is drawn as its own line of body text, wrapping to a
new page automatically past roughly 40-45 lines. This is the **default**
tool for a finished report, summary, or brief when the user hasn't named a
specific file format; prefer it over `docx_create` unless they explicitly
ask for something editable in Word.

Because it is plain-text-to-PDF and not a layout engine:

- There is no heading styling, bullet formatting, table support, or image
  embedding -- long lines are truncated (roughly 180 characters per line),
  so wrap or shorten text yourself rather than relying on the tool to do it.
- Simulate structure with plain-text conventions (a blank line between
  sections, a short all-caps or title-case line as a heading) since there's
  no markup support -- sending Markdown syntax will render literally as
  `#`, `**`, etc.
- If the user's request genuinely needs tables, embedded charts, or visual
  styling, this tool cannot deliver that -- either use `html_create`
  instead (see below) or say plainly what will be missing from the plain-text
  PDF.

## When NOT to reach for pdf_create: `html_create`

If the user wants something visually rich -- charts, colors, a styled
layout -- but hasn't asked for a specific downloadable file format,
`html_create` is the better fit, not `pdf_create`. It renders raw HTML
(and inline CSS/SVG/charts) directly, so it can express layout and visuals
that reportlab's plain-text canvas cannot. The result is served inline from
its `download_url` -- it opens as a page in the browser rather than
downloading a file, so present it to the user as a link to view, not a
file to download. Keep using `pdf_create` for a straightforward text
report or brief; reach for `html_create` specifically when visual richness
is the point and no particular file format was requested.

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
before reporting success. State any real limitation plainly: a scanned
page with no text layer, a truncated long line, a merge whose page order
might not match what the user expected.

Every `pdf_create`/`pdf_merge`/`pdf_split`/`pdf_edit` result includes a
`download_url` field only when a public URL is configured for this
deployment. Share that exact string verbatim when present; if it's absent,
say plainly that the file was created but no shareable link is available
right now -- never construct or guess one.

## Related skills

`ocr-documents` (text extraction from scanned pages), `nano-pdf` (small,
precise edits to an existing PDF's content), `docx` (Word documents),
`xlsx` (spreadsheets), `powerpoint` (slide decks).
