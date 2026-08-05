---
name: xlsx
display_name: XLSX Spreadsheets
description: Analyze and create spreadsheets with Docket's artifact-based xlsx tools, preserving formulas and types.
kind: builtin
platform_key: builtin.hermes.xlsx
activation: {mode: optional}
required_capabilities: {all_of: [documents.read, documents.write]}
steps: [{order: 1, name: Inspect, description: Identify sheets, ranges, and formulas.}, {order: 2, name: Transform, description: Preserve types and formulas.}, {order: 3, name: Verify, description: Reopen and check key cells.}]
warnings:
  - xlsx_edit writes a raw value into each A1 cell reference given -- it does not evaluate formulas, so a formula string is stored as text, not computed.
  - Never construct or guess a download link; only share the exact download_url a tool call returned, and say plainly if it is absent.
---
Docket's spreadsheet support is artifact-based: `xlsx_read`, `xlsx_create`,
`xlsx_edit`, `xlsx_render`, and `xlsx_validate` all take (or return) an
`artifact_id`, never a filesystem path. There is no external recalculation
engine wired in -- know what that means for formulas before promising a
"verified" workbook.

## When to use this skill

Use `xlsx_create` for tabular or structured data, or whenever the user
explicitly asks for Excel or a spreadsheet. If the user just wants a
readable summary or report and hasn't asked for a spreadsheet, prefer
`pdf_create` instead -- don't reach for xlsx just because the underlying
data happens to be tabular. Use `xlsx_read` before editing anything, and
`xlsx_edit` only for small, targeted cell changes to a workbook you already
have as an artifact.

## Reading (`xlsx_read`)

Returns each sheet's name and its rows (each row a list of cell values),
capped to the first 100 rows and 50 columns per sheet -- a larger workbook
is read only partially, so say so rather than implying you inspected the
whole sheet. Cells holding a formula come back as the formula string itself
(e.g. `=SUM(B2:B9)`), not a computed value -- there's no separate
data-only pass, so don't report a formula cell's displayed number unless
you can compute it yourself from the surrounding values, and say when a
figure is your own calculation rather than the workbook's stored result.

## Creating (`xlsx_create`)

Takes a `title` (used as the sheet name, truncated to 31 characters -- the
Excel sheet-name limit) and `rows`, an array of arrays. Each inner array
becomes one row, appended in order starting at row 1; there is no header
styling, column-width control, cell-format (currency/percentage/date), or
multi-sheet support -- one call produces exactly one sheet with plain
values.

- Put column headers as the first row of `rows` yourself; the tool applies
  no automatic header formatting.
- A formula belongs in a cell as a plain string starting with `=` (e.g.
  `"=SUM(B2:B9)"`) -- openpyxl (which backs this tool) writes it as a
  formula, but nothing in this pipeline recalculates it afterward, so its
  cached value will read back as empty/None until opened in a real
  spreadsheet application. Tell the user this if they're asking you to
  confirm a computed result -- you cannot verify a formula's output inside
  this tool chain, only that the formula string is well-formed.
- Keep every row the same shape (same number of columns) unless a ragged
  table is genuinely what the user wants -- openpyxl will accept ragged
  rows silently, which reads as a data error to anyone opening the file.
- If the user needs multiple sheets, tabs, or per-cell formatting/colors,
  say plainly that this tool produces a single plain sheet and ask whether
  a single-sheet version is acceptable, rather than silently dropping the
  extra structure.

## Editing (`xlsx_edit`)

Takes an `artifact_id`, an optional `sheet` name (defaults to the first
sheet), and a `cells` object mapping A1-style references to new values
(e.g. `{"B2": 42, "C2": "=B2*1.1"}`). Each reference is written directly to
that cell, overwriting whatever was there -- there is no merge-cell
awareness, no formula recalculation, and no validation that a formula
string is syntactically valid Excel. Because of that:

- Double-check the target sheet name against what `xlsx_read` reported --
  passing a sheet name that doesn't exist in the workbook fails outright.
- Only touch the specific cells the user asked about; resist rewriting a
  whole range when a targeted edit will do, since there's no undo and no
  automatic re-verification of untouched cells.
- If the edit changes a value that other formulas depend on, tell the user
  those dependent cells will show stale results until the workbook is
  opened and recalculated by a real spreadsheet program -- this pipeline
  cannot do that recalculation for them.

## Validating (`xlsx_validate`)

Runs the same read as `xlsx_read` and additionally counts how many cell
values start with `=` across all sheets, returning `formula_count` and a
`valid: true` flag. This is a structural sanity check (does the workbook
open, how many formulas does it contain), not a correctness check --
`valid: true` does not mean the formulas evaluate to the right numbers, or
even that they're syntactically correct Excel. Don't tell the user their
formulas were "verified correct"; say what was actually checked.

## Verifying and delivering

After a create or edit, run `xlsx_read` (or `xlsx_validate`) on the result
and confirm the rows/cells contain what you intended -- since there is no
recalculation step in this pipeline, remind the user that formula results
will appear once the file is opened in Excel/LibreOffice/Google Sheets, not
before.

Every `xlsx_create`/`xlsx_edit` result includes a `download_url` field only
when a public URL is configured for this deployment. Share that exact
string verbatim when present; if absent, say plainly that the spreadsheet
was created but no shareable link is available right now rather than
inventing one.

## Related skills

`docx` (Word documents), `pdf-documents` (the default report format),
`powerpoint` (slide decks).
