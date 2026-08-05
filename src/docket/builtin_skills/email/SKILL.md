---
name: email
display_name: Email
description: Search, summarize, draft, and send email through the organization's connected Gmail/Outlook account, with recipient and privacy checks before anything goes out.
kind: connector
platform_key: connector.email
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Search, description: Find the relevant message or thread using the mailbox's own search syntax, not a guess.}, {order: 2, name: Draft, description: Preserve thread context, recipients, and attachments; compose the exact final text.}, {order: 3, name: Send, description: Confirm recipients and content, then send and verify delivery — never send implicitly.}]
warnings: ["Sending, replying, or forwarding email delivers immediately and irreversibly the moment the action succeeds — there is no undo, and Docket has no draft-then-review step separate from actually calling the send action unless you deliberately use a create-draft action instead.", "Always verify the full recipient list (To, Cc, and especially Bcc) before sending — a reply-all or a stale Cc list carried over from a forwarded thread is the most common way an email reaches someone it shouldn't.", "Replying without the correct thread/message reference sends a disconnected new message instead of a threaded reply, breaking the recipient's view of the conversation and often confusing them about context.", "Attachments on an incoming message are typically fetched with a separate action from the message body — don't assume message content includes attachment bytes inline.", "Forwarding a thread carries every quoted message in that thread along with it, including anything sensitive earlier participants said expecting a narrower audience — read the full quoted history before forwarding, not just the latest message."]
---
There is no IMAP/SMTP client, no CLI mail tool, and no raw credentials available to Docket for this — every email action (search, read, send, reply, forward, label/move, delete) is a Composio action against the organization's connected Gmail or Outlook account, run through `COMPOSIO_EXECUTE`. The lessons below are the same hard-won email lessons a terminal-based mail client teaches (thread handling, attachment gotchas, the gap between drafting and sending) translated into how they show up when email is a single governed tool call instead of a sequence of CLI commands you fully control.

## Finding the right action

There's no curated shortcut for email — discover every time, and be specific about the provider if the organization has more than one connected (Gmail and Outlook have different action families even for the same concept):

1. `COMPOSIO_SEARCH_TOOLS` with a plain-English `use_case` — e.g. "search Gmail messages matching a query", "read the full body of an email message", "send an email", "reply to an email in the same thread", "forward an email to a new recipient", "list labels on an email account", "move an email to a folder", "download an email attachment". It returns the matching provider-specific action(s).
2. `COMPOSIO_GET_TOOL_SCHEMAS` on the returned slug(s) — pay close attention to whether the action expects a plain-text body, an HTML body, or both, and what field it uses to reference "the message being replied to."
3. `COMPOSIO_EXECUTE` with `{tool_slug, arguments}`, passing `connected_account_id` explicitly if more than one mail account is connected for the organization.

## Search

- Gmail's connected actions accept Gmail's own query syntax inside the query argument — `is:unread`, `from:name@company.com`, `newer_than:7d`, `has:attachment filename:pdf`, boolean combinations of these. Outlook-family actions typically take separate structured filter fields (from, subject, date range) instead of one query string — check the schema rather than assuming Gmail syntax works against an Outlook-backed connection.
- Search before drafting anything. If the task is "reply to X's email about Y," find the actual message first — don't compose a reply from a paraphrased memory of what the message probably said.
- When summarizing a thread, read every message in it, not just the most recent — the ask or the decision being referenced might be several messages back.

## Draft

- **Preserve the thread.** A reply action needs the correct message or thread identifier to set proper threading headers — get this from the search/read result you just fetched, not from a message ID a human typed into chat that you haven't independently verified.
- **Match the format.** If you're replying inside an HTML-formatted thread, a plain-text reply can render oddly for the recipient; check whether the discovered action supports an HTML body when the context calls for it (a report with formatting, a table) versus when plain text is genuinely simpler and clearer.
- **Compose the exact final text before drafting the call.** Because sending is a single governed step with no separate "review the draft, then confirm" round trip built into the tool itself, treat your own drafted body as the thing a human is approving — don't plan to "clean it up after."
- **Attachments are a separate concern from the body.** Downloading an attachment from an incoming message is typically its own action referencing the message and the specific attachment; attaching a new file to an outgoing message likewise needs the file content/reference in the shape the send action's schema expects — never assume attachment handling is automatic just because you referenced a file in the body text.
- **If a create-draft action exists and the user hasn't explicitly said "send it,"** prefer creating a draft over sending — that gives a human a chance to review the exact content in their own mail client before it goes anywhere, which is strictly safer than composing an approval-gated send call for something they only asked you to "draft."

## Send

- **Confirm the full recipient list before calling the send action** — To, Cc, and Bcc. A reply pulled from a long-running thread can carry a Cc list that's grown to include people who no longer need or should see the next message; state the recipients back explicitly rather than silently carrying forward whatever the thread happened to include.
- **Reply vs. reply-all is a real decision, not a default.** Don't reply-all reflexively just because the original message had multiple recipients — match what the human actually asked for.
- **Forwarding carries the entire quoted history with it.** Before forwarding a thread, read what's quoted underneath the latest message — forwarding to a new, wider, or external audience can expose earlier remarks that participants made expecting a narrower readership.
- **Sending is immediate and irreversible.** Once the send action succeeds, the message is delivered — there is no recall in general, and even providers that offer a short "undo send" window are not something this tool surface exposes. Get the content right before the call, not after.
- **Verify delivery, don't just trust a success response blindly.** If the action's response includes a message/thread ID, that's your confirmation — report it back rather than assuming success from the absence of an error alone.

## Gotchas specific to how Docket runs this

- **Approval gate.** Search and read are read-only and execute immediately. Send/reply/forward actions on Gmail and Outlook sit in Composio's external-comms surface and pause for human approval before the message actually leaves — but that pause is not a substitute for you getting the content and recipients right; it's a second check, not the only one. Draft-creation, labeling, and moving messages are typically not on the forced-approval path but remain fully audited.
- **Provider differences are real, not cosmetic.** Gmail's label model (a message can have many labels, "read/unread" is itself a label) is different from Outlook's folder model (a message lives in one folder at a time) — don't port assumptions from one provider's mental model onto the other's action schema.
- **Multiple connected mail accounts** require explicit `connected_account_id` on `COMPOSIO_EXECUTE` — guess wrong and the call is rejected as ambiguous rather than silently picking one.
- **This is not the Slack-facing agent gateway.** If the organization also lets people email the agent directly (a separate inbound gateway), that's a different mechanism from this skill — this skill covers the agent actively operating a connected mailbox on request, not the reverse channel of people emailing Docket.
- **Never surface a credential.** OAuth tokens or app passwords backing the connected mail account live inside Composio; never ask a user to paste one into chat, and never echo one back even in a debugging context.
