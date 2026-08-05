---
name: ocr-documents
display_name: OCR and Documents
description: Extract text from scanned images with Docket's ocr_extract tool, preserving confidence and uncertainty.
kind: builtin
platform_key: builtin.hermes.ocr_documents
activation: {mode: optional}
required_capabilities: {all_of: [documents.read]}
steps: [{order: 1, name: Detect, description: Determine whether OCR is needed.}, {order: 2, name: Extract, description: Run OCR and retain page references.}, {order: 3, name: Review, description: Flag low-confidence text.}]
warnings:
  - ocr_extract runs on a single scanned image/page artifact -- for a multi-page scanned PDF, render or split each page to an image artifact first and run it per page.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
---
Docket's OCR support is one tool: `ocr_extract`, which takes a single
image (or scanned-page) `artifact_id` and returns extracted text plus a
confidence flag. There is no multi-language configuration, no bounding-box
output, no table-structure detection, and no batch mode in this pipeline --
know the actual shape of what this tool returns before promising more than
it delivers.

## Step 1: Determine whether OCR is actually needed

Not every PDF page needs OCR. First try `pdf_extract` (from the
`pdf-documents` skill) on the document -- if it returns real text for a
page, that page has a native text layer and OCR would be redundant, slower,
and lower-fidelity than the text already embedded in the file. Only fall
back to OCR when `pdf_extract` returns an empty or clearly garbled string
for a page, which is the signal that the page is a scanned image with no
underlying text layer. Don't run OCR reflexively on every document; check
the cheap path first.

## Step 2: Get the page onto an image artifact

`ocr_extract` operates on an image artifact, not a PDF page directly. For a
scanned PDF, render the specific page you need with `pdf_render` first
(from the `pdf-documents` skill) to produce a PNG image artifact, then pass
that artifact's id to `ocr_extract`. There is no tool in this pipeline that
runs OCR across an entire multi-page PDF in one call -- for a multi-page
scanned document, plan on one `pdf_render` + `ocr_extract` pair per page,
and keep track of which page each extracted block of text came from so you
can cite it accurately.

## Step 3: Extract and preserve uncertainty

`ocr_extract` returns the recognized `text` and a `confidence` field. Treat
low-confidence or ambiguous output as exactly that in your response --
don't silently "clean up" or guess-correct OCR output that looks like it
might be wrong (a smudged digit, a character that could be an "O" or a
"0"). If a passage reads as ambiguous or nonsensical, say so and quote what
the tool actually returned rather than presenting a confident guess as fact.
When you do need to interpret an ambiguous OCR result (e.g. inferring a
digit from context), say plainly that you're inferring it and why, so the
user can distinguish your inference from the tool's direct output.

## Step 4: Keep page references

When you're extracting from multiple pages of a scanned document, tag each
extracted block with the page number it came from before handing a summary
back to the user. This matters doubly here because OCR text has no
guaranteed one-to-one mapping back to layout position the way a native PDF
text layer might -- the page number is often the only anchor a user has to
go verify a specific claim against the source scan.

## What this skill is not for

- Text-based PDFs and Word/Excel/PowerPoint files with native text --
  those go through `pdf_extract`, `docx_read`, `xlsx_read`, or `pptx_read`
  directly; running OCR on them wastes a step and loses structure (tables,
  formatting) those readers already preserve.
- Editing a scanned document's content -- OCR only reads; if the user wants
  to change something on a scanned page, that page has no editable text
  layer to change in the first place, and the honest answer is that the
  underlying image would need to be replaced or annotated, not edited as
  text.
- Producing a new document from OCR output -- once you have clean, verified
  text, hand it to `docx_create`, `pdf_create`, or another format tool as
  appropriate; `ocr_extract` itself only reads.

## Related skills

`pdf-documents` (render a page to an image before OCR, and everything else
PDF), `nano-pdf` (small text fixes -- only applicable once the content has
a real text layer, not to a scanned image), `docx` and `powerpoint` (native
text extraction for their respective formats).
