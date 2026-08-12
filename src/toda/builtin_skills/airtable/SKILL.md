---
name: airtable
display_name: Airtable
description: Query and update authorized Airtable bases with explicit record scope through the Composio Airtable connector.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.airtable
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Identify, description: Confirm the exact base, table, and field names from the schema, not memory.}, {order: 2, name: Query, description: Read the minimal record set with a precise filter before considering any write.}, {order: 3, name: Mutate, description: Apply only the approved change, to only the resolved record IDs, and verify by re-reading.}]
warnings: ["Airtable field names in responses can be missing entirely when a field's value is empty for that record — a missing key means empty, not that the field doesn't exist on the table.", "Writing a single-select value that isn't already one of the field's defined options fails unless the action explicitly opts into creating new options — check the table schema before writing a status/category value.", "Airtable rate limits are per base (roughly five requests per second), not per organization or per token — hammering one base while another sits idle will still throttle.", "Record IDs (rec...) are the only safe handle for a specific row — resolve them from a filtered query first; never construct or guess a record ID.", "Deleting Airtable records has no undo through the API — always state the exact filter and record count back to the requester before a bulk delete executes."]
---
Toda never calls Airtable's REST API directly with a token. Every Airtable operation — listing bases, reading table schema, querying records, creating or updating rows, deleting records — runs exclusively as a Composio action through `COMPOSIO_EXECUTE`, against whichever `connected_account_id` the organization has linked to the `airtable` toolkit. There's no personal access token in your context and no direct HTTP call to construct; Composio's Airtable integration already holds the credential and talks to Airtable's API on your behalf.

## Finding the right action

1. Call `COMPOSIO_SEARCH_TOOLS` with a plain-English `use_case` — e.g. "list Airtable bases", "get the schema for an Airtable table", "list records in an Airtable table filtered by a field value", "create a record in an Airtable table", "update fields on an Airtable record", "delete an Airtable record". It returns the matching action slug(s), a suggested plan, and known pitfalls (Airtable-specific ones like formula encoding or batch size limits may already be surfaced there).
2. Call `COMPOSIO_GET_TOOL_SCHEMAS` on the returned slug(s) to see the exact arguments the action expects — in particular whether it wants a raw filter object, an Airtable formula string, or a list of typed field values.
3. Call `COMPOSIO_EXECUTE` with `{tool_slug, arguments}`. If more than one Airtable account is connected for the organization, pass `connected_account_id` explicitly.

`composio_search`/`composio_describe` are a narrower, this-organization-only fallback when you already know you want an Airtable action and don't need the full catalog search.

## Workflow

1. **Resolve the base and table first.** Never assume a base ID (prefixed `app`) or table ID (prefixed `tbl`) from earlier context is still correct — bases get renamed and re-organized. If the user gave you a name instead of an ID, search/list bases (and that base's tables) before doing anything else.
2. **Read the schema before mutating.** Fetch the table's field list and, for single-select/multi-select fields, their exact defined option names. This tells you the real field names (case and spacing matter), which fields are required, and which options already exist — the single biggest source of write failures is guessing a select-field value that isn't in the option list.
3. **Filter before you write.** For "update the record where X", query/filter first to resolve the specific record ID(s) (prefixed `rec`), then apply the update to those IDs. Never construct a record ID by pattern-matching one you've seen before.
4. **Prefer merge semantics.** When the chosen update action supports a partial/merge update (only the fields you pass change, everything else on the record is left alone) versus a full replace, default to the merge form unless you specifically intend to clear every field you don't mention.
5. **Batch related writes.** If you need to touch many rows, look for whether the discovered action accepts multiple records in one call rather than looping one `COMPOSIO_EXECUTE` call per row — Airtable's own API caps batch writes at ten records per request, and Composio's action schema will reflect that cap if it applies.
6. **Confirm before anything destructive.** For a bulk-delete request or "clear this view," state back the exact filter and how many records it matches, and let the approval step (below) be the actual gate — don't skip stating the scope even though the platform will still pause the call.

## Gotchas specific to how Toda runs this

- **Approval gate.** Reads (list bases, get schema, list/query records) execute immediately. Deletions are treated as destructive and pause for human approval automatically; ordinary creates/updates typically don't, but remain fully audited — state the intended change clearly in your own reasoning/response rather than adding a redundant chat confirmation on top of the platform's gate.
- **Empty fields vanish from responses.** If a record's JSON is missing an "Assignee" key, that means the field is empty for that record, not that the field was deleted from the table — always check the schema (not a sample record) before concluding a field doesn't exist.
- **Per-base scoping, not per-organization.** A connected Airtable account may only have access to specific bases (the equivalent of a personal access token's Access list). If an action fails with a permission error on one base while working fine on another, that's a scoping issue on the connected account — tell the user to check the base is included in the org's Airtable connection, not that Airtable is down.
- **Formulas are Airtable's query language, not Toda's.** If the action you're using takes an Airtable `filterByFormula`-style expression, build it the way Airtable expects (`{Field Name}='value'`, `AND(a, b)`, `OR(a, b)`, `FIND(needle, haystack)`) — malformed formula syntax fails silently as "no records matched" rather than as a clear parse error, so double-check the formula against the schema's real field names when a query returns suspiciously zero rows.
- **Pagination is Airtable's, not something you can raise.** List/query actions return a bounded page (Airtable's own cap is 100 records per page); if the schema/response includes a continuation token, loop on it rather than assuming a single call returns every row.
- **Never surface a token.** Whatever credential backs the connected account lives inside Composio — don't ask the user to paste a personal access token into chat, and don't echo one back even if it appears somewhere in a debugging context.
