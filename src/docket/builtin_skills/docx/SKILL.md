---
name: docx
display_name: DOCX Documents
description: Read and create Word documents with verified structure, using Docket's artifact-based docx tools.
kind: builtin
platform_key: builtin.hermes.docx
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Inspect, description: Read structure and styles.}, {order: 2, name: Edit, description: Apply the requested content change.}, {order: 3, name: Render, description: Verify the final document visually.}]
warnings:
  - docx_edit only replaces exact substring matches inside existing paragraphs -- it cannot restructure headings, tables, or add new sections.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
---
Docket's Word support is artifact-based: every operation takes an `artifact_id`
(never a file path) and returns a new or updated artifact. There is no
filesystem access to the underlying `.docx` bytes, so every change goes
through one of four tools -- `docx_read`, `docx_create`, `docx_edit`,
`docx_render` -- and nothing else. Don't describe or imply capabilities
(XML surgery, tracked changes, comments, templates) that these tools do not
expose.

## When to use this skill

Reach for `docx_create` only when the user explicitly asks for Word, a
`.docx`, or an "editable document." For an ordinary report, memo, or summary
where the user hasn't named a format, `pdf_create` is the system default --
do not switch to Word just because the content happens to be a document.
Use `docx_read` when the user hands you an existing Word file and wants its
content read, summarized, or checked. Use `docx_edit` for a small, targeted
text change inside a document you (or the user) already have as an artifact.

## Reading (`docx_read`)

`docx_read` returns two things: a flat list of paragraph strings (in
document order) and a list of tables, each table as a list of rows, each row
as a list of cell strings. There is no separate access to headings, styles,
run formatting, images, or headers/footers -- if the user needs to know
whether a paragraph is a heading, you can only infer it from wording and
position, not from a style attribute. Don't claim to have read structure
that the tool doesn't surface (font choices, tracked changes, comments --
none of that comes back from `docx_read`). When summarizing a table, keep
row order and don't silently drop empty cells; an empty string in a row is
still a cell the user may care about.

## Creating (`docx_create`)

`docx_create` takes a `title` and a `text` body. The title becomes a
level-1 heading; the body text is split on newlines and each line becomes
its own paragraph -- there is no markup for bullet lists, tables, bold/italic
runs, or page breaks. Practically:

- Write plain prose, one paragraph per line. Don't send Markdown syntax
  (`#`, `**`, `-`) expecting it to render -- it will show up as literal
  characters in the document.
- If the user needs a numbered or bulleted structure, simulate it with plain
  text prefixes ("1. ", "- ") since there is no real list formatting; say so
  if the user asks for something the tool genuinely can't produce (a styled
  table, embedded image, multi-column layout).
- Keep the `title` short and descriptive -- it is the only structural
  element the tool adds beyond the heading level.
- If the user's ask is really tabular data, prefer `xlsx_create` instead and
  say why; don't force a table into `docx_create`'s single text stream.

## Editing (`docx_edit`)

`docx_edit` takes an existing `artifact_id`, an `old_text` string, and a
replacement `text` string. It walks every paragraph, and inside any
paragraph containing `old_text` it does a literal find-and-replace across
that paragraph's runs. Implications:

- `old_text` must appear verbatim (whitespace and punctuation included) in
  at least one paragraph or nothing changes -- the tool reports how many
  paragraphs it changed, so check that number is what you expected (zero
  means your match string was wrong or the phrase doesn't exist).
- It cannot insert a new paragraph, delete a paragraph, reorder content, or
  touch table cells -- only in-place text substitution within paragraphs
  that already contain the match.
- For a change that spans a whole section, table, or heading, don't try to
  force it through `docx_edit`; tell the user this exceeds what surgical
  text replacement can do and offer to regenerate the affected section with
  `docx_create` instead (as a new document, since there's no partial-rebuild
  tool).

## Verifying and delivering

There is no direct visual render tool wired to a preview in this skill's
current tool surface beyond what `docx_render` is meant to provide once its
runtime is configured -- if it returns a `setup_needed` result, say so rather
than claiming you visually verified layout you didn't. Otherwise, after any
edit or create, do a quick `docx_read` pass on the result artifact and
confirm the paragraphs contain what you intended before telling the user
it's done.

Every `docx_create`/`docx_edit` result includes a `download_url` field only
when a public URL is configured for this deployment. Share that exact
string verbatim when present. If it is missing, tell the user plainly that
the document was created but no shareable link is available right now --
never fabricate a plausible-looking URL.

## Related skills

`pdf-documents` (the default format for a finished report), `xlsx` (tabular
data), `powerpoint` (slide decks), `ocr-documents` (extracting text from
scanned input before turning it into a Word document).
