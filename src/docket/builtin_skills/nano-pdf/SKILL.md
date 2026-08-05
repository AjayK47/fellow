---
name: nano-pdf
display_name: Nano PDF Editing
description: Make small, targeted PDF edits while preserving the rest of the artifact.
kind: builtin
platform_key: builtin.hermes.nano_pdf
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Inspect, description: Render and locate the target.}, {order: 2, name: Edit, description: Apply only the requested local change.}, {order: 3, name: Verify, description: Render the result and compare.}]
warnings:
  - There is no dedicated nano-pdf tool in Docket -- this skill is a discipline for using pdf_inspect, pdf_render, and pdf_edit together for small local changes, not a separate capability.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
---
This skill is not a separate tool -- it's a discipline for the smallest
possible PDF change, built entirely on the same artifact-based PDF tools as
the `pdf-documents` skill: `pdf_inspect`, `pdf_render`, `pdf_edit`, and (to
confirm the fix) `pdf_render` again. Use it whenever the request is "fix
this one date/name/typo/value on page N" rather than "produce a new
document" -- the goal is to change exactly what was asked and leave
everything else on the page untouched.

## When to use this skill vs. `pdf-documents`

If the user wants a brand-new document, a merge, a split, a big structural
change, or if the source content doesn't exist yet, that's the
`pdf-documents` skill's territory (`pdf_create`, `pdf_merge`, `pdf_split`).
Reach for the nano-pdf discipline specifically when there's an existing PDF
artifact and the requested change is small and local: correcting a name,
fixing a date, adjusting one figure, fixing a typo. If the requested change
spans multiple pages or restructures the document, say so and switch to the
`pdf-documents` tools instead of forcing it through a series of small edits.

## Step 1: Inspect before touching anything

Always start with `pdf_inspect` to confirm the page count and check for
encryption, then `pdf_render` on the specific page the user is asking
about. Look at the rendered image before making any claims about what's on
that page or proposing a fix -- don't rely on `pdf_extract`'s text alone
for this, since visual position, surrounding context, and formatting on the
page matter for a precise, minimal edit and text extraction won't show you
layout.

## Step 2: Make the smallest possible change

Docket's PDF editing surface here is `pdf_edit` -- treat any edit as a
targeted content substitution, not a rebuild. Before calling it:

- Confirm you know the exact existing text you're replacing, not just its
  approximate meaning -- an edit tool that can't find an exact match either
  fails or (worse) matches the wrong occurrence.
- Change only the specific value in question. Don't take the opportunity to
  "clean up" nearby text, reformat, or fix unrelated issues you notice --
  those are separate asks and expanding scope without being asked risks
  changing something the user didn't want touched.
- If `pdf_edit`'s capability isn't configured in this deployment, it will
  come back as a `setup_needed` result -- report that plainly rather than
  claiming the edit succeeded, and don't fall back to silently regenerating
  the whole document with `pdf_create` (that discards the original
  formatting and any content outside what you were asked to change).

## Step 3: Verify by re-rendering, not by assuming

After the edit, `pdf_render` the same page again and visually compare it to
the pre-edit render: confirm the target text actually changed to the
correct new value, and confirm nothing else on the page shifted, disappeared,
or changed formatting as a side effect. Don't tell the user the fix is
correct based on the tool call succeeding alone -- a successful call and a
visually correct page are different claims, and only the second is what the
user actually asked for.

## What this discipline is not for

- Bulk changes across many pages -- that's better served by inspecting the
  whole document's structure and using the broader `pdf-documents` tools
  deliberately, not repeating a "small edit" many times.
- Adding new content, tables, or images -- `pdf_edit` operates on existing
  text content, not layout construction.
- Anything the user describes as "regenerate" or "redo" rather than "fix" or
  "correct" -- that's a signal they want a new document, which belongs to
  `pdf-documents`'s `pdf_create`.

## Delivering the result

If the edited artifact has a `download_url`, share that exact string
verbatim -- never construct or guess one. If it's absent, say plainly that
the edit was made but no shareable link is available right now.

## Related skills

`pdf-documents` (full PDF creation, merge, split, form-fill), `ocr-documents`
(when the "page" in question is actually a scanned image with no text
layer to edit).
