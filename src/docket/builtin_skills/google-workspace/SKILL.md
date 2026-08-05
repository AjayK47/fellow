---
name: google-workspace
display_name: Google Workspace
description: Search and update authorized Gmail, Calendar, Drive, Sheets, and Docs content through the Composio Google connectors.
kind: connector
platform_key: connector.google.workspace
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Resolve, description: Identify the correct connected account and the exact object (message, event, file, sheet, doc).}, {order: 2, name: Read, description: Inspect permission-safe content before proposing any change.}, {order: 3, name: Write, description: Apply approved changes and verify the result.}]
warnings: ["Sending an email, sharing a Drive file, creating/deleting a Calendar event, or editing a Doc/Sheet are all real, often externally-visible actions — always confirm recipients, sharing scope, and content before the write executes.", "Google's own APIs are split across separate services (Gmail, Calendar, Drive, Sheets, Docs, People) with separate OAuth scopes — a connected account authorized for email may not have Calendar/Drive access, and vice versa; a scope error means the connection needs broader authorization, not that the action is broken.", "Calendar event times must be unambiguous ISO 8601 with an explicit timezone offset or Z — a bare local time without an offset is a common source of events landing an hour or a day off.", "Drive deletes typically move a file to trash by default rather than permanently erasing it — treat a permanent/skip-trash option as materially more destructive and confirm it's really intended before using it.", "Gmail search operators (is:unread, from:, newer_than:, has:attachment) are Gmail's own query syntax, not a Docket invention — get the operator syntax right or the search silently returns the wrong set of messages."]
---
Docket has no OAuth client, no `gws` CLI, and no local token file for Google Workspace — every Gmail, Calendar, Drive, Sheets, and Docs action is a Composio action run through `COMPOSIO_EXECUTE`, against whichever Google connected account(s) the organization has authorized. There is no setup script the agent runs; the organization's admin connects Google Workspace once through Docket's own Connections dashboard, and Composio holds the resulting OAuth tokens.

## Connected accounts are per-service, not one blanket "Google" account

Google's OAuth model — and Composio's toolkit split — treats Gmail, Google Calendar, Google Drive, Google Sheets, and Google Docs as separate toolkits, each with its own scopes and its own connected account. An organization might have Gmail and Calendar connected but not Drive, or vice versa. Before attempting an action:

- Check which Google toolkits are actually connected (a listing of connected integrations is available at the harness level) rather than assuming "Google Workspace" implies every service is authorized.
- If the needed toolkit isn't connected, tell the user plainly which Google service needs to be connected and point them at Docket's Connections page to add it — Docket cannot initiate that OAuth grant from inside a conversation; a human has to approve the consent screen.
- A permission/scope error on an otherwise-working connected account usually means that specific service's scope was never granted (e.g., Calendar was connected read-only, or Drive was never included) — the fix is broadening the connection in the dashboard, not retrying the call.

## Finding the right action

There's no curated shortcut for Google Workspace — use discovery every time:

1. `COMPOSIO_SEARCH_TOOLS` with a plain-English `use_case` describing exactly what you need — be specific about the service, since "search email," "list calendar events," "upload a file to Drive," "read a Google Doc," and "update a Sheets range" all live in different toolkits with different action families. Good examples: "search Gmail messages with a query", "send an email via Gmail", "list upcoming Google Calendar events", "create a Google Calendar event with attendees", "search files in Google Drive", "share a Google Drive file with an email address", "append rows to a Google Sheet", "create a new Google Doc".
2. `COMPOSIO_GET_TOOL_SCHEMAS` on the returned slug(s) — Gmail send/reply, Calendar create, and Sheets update in particular have several optional-but-important fields (HTML vs. plain body, timezone, value input mode) that are easy to get subtly wrong.
3. `COMPOSIO_EXECUTE` with `{tool_slug, arguments}`, passing `connected_account_id` explicitly whenever more than one account for that toolkit is connected.

## Gmail

- **Search** uses Gmail's own query syntax inside the query argument — `is:unread`, `from:name@company.com`, `newer_than:7d`, `has:attachment filename:pdf`, and boolean combinations of these. Get this syntax right; a malformed query returns an empty or wrong result set without necessarily erroring.
- **Reading a message** returns headers plus body — check whether the discovered action returns plain text, HTML, or both, and prefer plain text for anything you're about to summarize.
- **Sending and replying** are real, delivered, externally-visible actions the moment the call succeeds — there is no draft-then-send-later step inside a single `COMPOSIO_EXECUTE` call. Confirm recipients (To/Cc/Bcc), subject, and body content before calling the send action; if the discovered action supports creating a draft instead of sending immediately, prefer the draft action when the user hasn't explicitly said "send it."
- **Replying in-thread** requires referencing the correct message/thread ID so Gmail sets `In-Reply-To`/`References` correctly — a reply sent as a new message instead of a threaded one is confusing to the recipient and breaks their view of the conversation.
- **Attachments** on an incoming message are typically fetched by a separate "get attachment" action referencing the message and attachment ID — don't assume the message-read action inlines attachment bytes.
- **Labels** — Gmail's read/unread and folder-like behavior is implemented via labels, not a status field; check the toolkit's label-list action if you need the exact label name/ID before modifying one.

## Calendar

- **Always use explicit timezone-qualified timestamps** — either an offset (`2026-03-01T10:00:00-06:00`) or UTC `Z`. A bare local time with no offset is the single most common cause of an event landing at the wrong hour, or on the wrong day for someone in a different timezone.
- **Creating an event with attendees** sends real calendar invites to those email addresses the moment the action succeeds — confirm the attendee list before executing, especially for anyone outside the organization.
- **Deleting an event** removes it (and, depending on the action, may notify attendees of the cancellation) — confirm this is intended, particularly for a recurring event where "delete" might mean one occurrence or the whole series depending on the arguments the discovered action exposes.

## Drive

- **Uploads** typically auto-detect MIME type from the file content/extension; **downloads** of native Google formats (Docs, Sheets, Slides) require an export MIME type (e.g., PDF, plain text, CSV) since there's no "raw" format for those.
- **Sharing** a file (adding a permission for an email, a domain, or "anyone with the link") is an access-control change with real consequences — confirm the intended role (reader/writer/commenter) and audience before executing, and default to the narrowest scope that satisfies the request.
- **Deletion defaults to trash** (recoverable for a period) unless the discovered action explicitly supports a permanent/skip-trash flag — prefer the trash default unless the user is explicit about wanting a permanent delete.

## Sheets and Docs

- **Sheets** reads/writes operate on an `A1`-notation range (e.g. `Sheet1!A1:D10`) — get the sheet name and range right, and be clear about whether you're overwriting a range (`update`) versus adding new rows at the end (`append`), since those are different actions with different blast radii.
- **Docs** — appending text to an existing document is additive; check whether the discovered action supports inserting at a specific location versus only at the end before assuming precise placement is possible.

## Gotchas specific to how Docket runs this

- **Approval gate.** Read-only actions (search, get, list) execute immediately. Gmail send/reply live in Composio's external-comms surface and pause for human approval before delivering; most Calendar/Drive/Sheets/Docs writes are fully audited but not automatically forced through approval by keyword alone — treat every send, share, or delete as needing the same explicit human confirmation in conversation regardless of whether the platform additionally pauses it.
- **No blanket Google account.** Reiterating because it's the most common source of confusion: a working Gmail connection says nothing about whether Calendar, Drive, Sheets, or Docs are also connected for this organization.
- **Rate limits are Google's**, and vary per API (Gmail, Calendar, Drive, Sheets, Docs each have their own quota) — a burst of many rapid calls to the same service can be throttled even if the organization is nowhere near account-wide limits.
- **Never surface a token.** OAuth credentials for every connected Google service live inside Composio; don't ask a user to paste a client secret, refresh token, or access token into chat, and don't echo one back even if it appears in a debugging context.
