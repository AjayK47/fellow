---
name: notion
display_name: Notion
description: Read and maintain authorized Notion pages and databases through the Composio Notion connector.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.notion
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Locate, description: Resolve the exact page or database via search, never a guessed ID.}, {order: 2, name: Inspect, description: Read current blocks, properties, and the data source schema before touching anything.}, {order: 3, name: Update, description: Make the smallest approved write, then re-read to confirm it landed.}]
warnings: ["A 404 on a page/database that you know exists almost always means the organization's Notion integration was never shared with that page — this is a Notion permissions problem, not a broken connection.", "Notion's 2025-09-03 API split databases into database_id (for creating pages) and data_source_id (for querying) — using the wrong one on the wrong endpoint fails even though both IDs look like valid UUIDs.", "Writing a select/status value that is not already one of the field's defined options fails unless the action explicitly allows creating new options — check the schema first rather than guessing labels.", "Property writes must match Notion's typed property shapes exactly (title vs rich_text vs select vs multi_select vs date) — a plain string in the wrong slot is silently rejected or miswritten.", "Never paste the organization's Notion connection details or any token-like string into a chat message or artifact — connected-account credentials stay inside Composio."]
---
Toda never talks to Notion's REST API directly. Every Notion action — search, read a page, query a database, append blocks, update properties — runs exclusively as a Composio action through `COMPOSIO_EXECUTE`, using whichever `connected_account_id` the organization has linked to the `notion` toolkit. There is no bearer token, no `Notion-Version` header, and no `ntn` CLI available to you; those are implementation details Composio's Notion integration already handles server-side.

## Finding the right action

Notion exposes a lot of surface area (pages, blocks, databases/data sources, users, comments, file uploads). Don't guess a Composio slug from memory — the exact action names Composio ships for Notion change over time and guessing wastes a round trip on a failed `COMPOSIO_EXECUTE` call.

1. Call `COMPOSIO_SEARCH_TOOLS` with a plain-English `use_case`, e.g. "search Notion pages by title", "read a Notion page's content as blocks", "query a Notion database for items with Status = Active", "append a paragraph block to a Notion page", "update a property on a Notion database item". It returns matching action slugs plus a recommended plan and known pitfalls.
2. Call `COMPOSIO_GET_TOOL_SCHEMAS` with the slug(s) it returned to get the exact input shape — Notion's own API distinguishes creating a page from writing markdown/blocks from patching properties, and the schema tells you which fields each action actually expects.
3. Only then call `COMPOSIO_EXECUTE` with `{tool_slug, arguments}`. If the organization has more than one Notion connected account, pass `connected_account_id` explicitly — otherwise the executor will reject an ambiguous call.

If the org's connected toolkits are already known to be limited, `composio_search`/`composio_describe` are a narrower, faster alternative scoped to just this organization's connections — reach for those once you already know you want something Notion-shaped and don't need the broader catalog search.

## Workflow

1. **Locate before you touch anything.** Search by title/query rather than assuming you have a page or database ID memorized from earlier context — Notion IDs are opaque UUIDs, and a stale one from a prior conversation may point at the wrong page.
2. **Read the current state.** For a page, fetch its content/blocks before editing so you know what already exists — appending is additive, but a property update or block edit should never blindly overwrite content you haven't actually inspected. For a database, fetch its schema (property names, types, and — for select/status fields — the exact set of valid option names) before writing any record into it.
3. **Match the property shape.** Notion properties are typed: a title is a rich-text array, a select is `{ "name": "Value" }`, a multi-select is an array of `{ "name": "Value" }` objects, a date has a `start` (and optional `end`), a checkbox is a bare boolean, a relation is an array of related page IDs. Whatever schema `COMPOSIO_GET_TOOL_SCHEMAS` returns for the write action is authoritative — don't reuse a shape from a different property type.
4. **Databases vs. data sources.** If the organization is on Notion's post-2025-09-03 API, what used to be called a "database" is modeled as a database (container) plus one or more data sources (the actual queryable table). Creating a page inside it uses the database's parent ID; querying/filtering it uses the data source's ID. If a query or create call fails with a not-found error on an ID you're confident is correct, try the other ID — you may have the right object but the wrong ID field for that specific action.
5. **Confirm the write landed.** After any create/update/append, do a follow-up read (or rely on the response Composio returns) and summarize exactly what changed before telling the user it's done.

## Gotchas specific to how Toda runs this

- **The 404-means-unshared trap.** The single most common Notion failure isn't auth — it's that the page or database was never connected to the organization's Notion integration inside Notion itself (a human has to do this from the page's own sharing menu; Toda cannot grant itself access). If `COMPOSIO_EXECUTE` returns a not-found error on an object a human insists exists, tell them to check that the page/database is shared with the connected integration in Notion, not that the connection is broken.
- **Approval gate.** Reads (search, get page, query database) execute immediately. Any write — creating a page, updating properties, appending blocks, archiving/deleting — goes through the same risk classification as every other Composio action: an explicitly destructive slug (delete/remove/archive-as-delete) pauses for human approval; most ordinary create/update writes do not, but are still fully audited. Don't ask the user for a separate confirmation before calling `COMPOSIO_EXECUTE` on a routine update — the platform's own approval gate is the confirmation step, not a duplicate one you add in chat.
- **Rate limits are Notion's, not Toda's.** Notion enforces roughly three requests per second per integration. If you're doing several related reads/writes (e.g., updating ten rows), don't fire them all in the same turn without expecting some to be throttled — space out or batch where the chosen action supports batch input.
- **Minimal, reversible writes.** Prefer appending a new block over rewriting an entire page body, and prefer updating just the changed property over replacing the whole properties object, so a human reviewing the change later can see exactly what Toda touched.
- **No raw markdown assumptions.** Whether an action accepts a markdown string, a structured blocks array, or typed properties depends entirely on which Composio action you picked — always defer to the schema from `COMPOSIO_GET_TOOL_SCHEMAS` rather than assuming Notion's markdown extensions (callouts, toggles, mentions) are supported by whatever action you're calling.
