---
name: mcp-oauth
display_name: MCP OAuth Gateway
description: Understand and troubleshoot the OAuth-backed lifecycle of connected Composio integrations — scoped, expiring, revocable credentials the agent never holds directly.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.mcp_oauth
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Authorize, description: Point the human at Toda's Connections dashboard to grant only the scopes the task actually needs.}, {order: 2, name: Store, description: Treat expiry, refresh, and connected-account status as Composio's concern, never something to expose in chat.}, {order: 3, name: Revoke, description: Treat a denied, expired, or revoked connection as a normal state that needs reauthorization, not a bug to work around.}]
warnings: ["Toda never runs an OAuth authorization-code exchange, holds a client secret, or writes a token file itself — every credential for every connected integration lives inside Composio, reached only through a resolved connected_account_id.", "Never ask a user to paste a client ID, client secret, access token, refresh token, or API key into a chat message — the only supported way to connect or reconnect an integration is Toda's own Connections dashboard, which talks to Composio directly.", "A PermissionError on integration_execute citing no matching connected account almost always means the needed toolkit was never connected, or was disconnected/revoked — this is a reauthorization problem to hand back to a human, not something to retry past.", "When more than one connected account exists for the same toolkit, integration_execute requires an explicit connected_account_id — an ambiguous match is rejected rather than guessed, by design.", "Requesting the broadest possible access for convenience is the wrong default — when a human is setting up a new connection, point them toward the narrowest auth scheme/scope that satisfies the actual task."]
---
Every other connector skill in this library assumes a working connected account and spends its guidance on what to do with it. This skill is about the layer underneath that: how an integration gets connected in the first place, what "the connection is scoped/expiring/revocable" actually means in this harness, and how to recognize and respond to a connection that's missing, denied, expired, or revoked. There is no separate "MCP server" the agent connects to directly, and no manual OAuth dance the agent performs from inside a conversation — Toda's own backend and Composio handle the entire OAuth lifecycle; the agent only ever reaches a connected integration through a resolved `connected_account_id` passed to `integration_execute`.

## How a connection actually comes to exist

An organization's admin connects an integration once, from Toda's own Connections dashboard, in one of two ways depending on what the target service supports:

- **OAuth** — the dashboard finds-or-creates an auth config for the toolkit and opens a hosted authorization link; the human approves scopes on the provider's own consent screen, and Composio stores the resulting access/refresh tokens against a `connected_account_id`. This is the path for services like Notion, Google Workspace, GitHub, and most modern SaaS integrations.
- **Credential-based (API key / bearer token / basic auth)** — for services without a usable OAuth flow, the dashboard collects the credential directly and Composio activates the connected account synchronously, with no redirect to wait on.

Either way, the agent is never in the loop for the initial grant — you cannot open an authorization URL, exchange a code, or write a token yourself, and there is no reason to try. Your role when a human asks "how do I connect X" is to point them at the Connections page and describe, at a plain-English level, what access they're about to grant — not to attempt any part of the flow yourself.

## Least privilege, in practice

When a human is about to connect a new integration (or reconnect one) for a task you're helping with:

- Name the narrowest thing the task actually needs — "read access to search issues" versus "write access to create and close issues" are different asks, and a human deciding what to authorize benefits from knowing which one your task actually requires.
- If a credential-based scheme offers a scoped/restricted key option (common for services with fine-grained API key permissions) versus a full-access key, say so — the dashboard's connect flow surfaces whatever schemes the toolkit supports, but you're the one who knows what the task in front of you actually needs.
- Don't ask for broader access "just in case." A connection scoped to what's needed today is easier to reason about later, and easier to revoke without collateral impact on unrelated work.

## Recognizing expiry, denial, and revocation as normal states

A connected account is not a permanent fact — tokens expire, humans revoke access, and a provider can invalidate a session server-side. None of these are bugs in Toda; they're the normal lifecycle of a scoped, revocable credential, and the correct response is always the same shape: tell the human plainly what happened and point them back to the Connections dashboard to reauthorize.

Signals to recognize, from the tool surface you actually have:

- **`integration_execute` raises a permission error citing no matching connected account** for the toolkit the action needs. Read this as "nothing is connected for this toolkit" or "the connection that existed is no longer active" — not as a transient failure worth retrying.
- **An action that used to work starts failing uniformly** across multiple calls to the same toolkit. That pattern points at an expired or revoked connection rather than a one-off bad request — don't keep retrying the same call expecting a different result once you've seen it fail more than once for what looks like an auth reason.
- **Multiple connected accounts for one toolkit and an ambiguous-match error.** This isn't expiry — it means the organization has more than one account connected for that service and you need to ask which one, or pass the specific `connected_account_id` if you already know it from context.

In every one of these cases, your job is the same: explain the situation in plain terms ("the GitHub connection for this organization doesn't appear to be active — you'll need to reconnect it from the Connections page"), not to work around it, retry indefinitely, or fabricate a result as if the call had succeeded.

## Gotchas specific to how Toda runs this

- **No tokens ever enter your context.** Whatever Composio stores for a connected account — access tokens, refresh tokens, API keys — is never returned to you as part of a normal tool call, and it should never appear in anything you write back to a user, an artifact, or a log-shaped message. If a debugging situation ever surfaces something that looks like a live credential, don't repeat it back — describe the situation without quoting the value.
- **Reauthorization is a human action, full stop.** There is no tool in your surface that opens an authorization URL, exchanges a code, or writes connection state — if a human asks you to "just reconnect it," the honest answer is that you can tell them exactly where to go and what to expect, but the click-through itself is theirs to do.
- **Every external application uses the same integration path.** Vendor skills may name GitHub, Linear, ClickUp, or another product because their semantics differ, but action discovery, schema inspection, execution, connection selection, and verification all use the shared Composio tools. There are no vendor-specific credential or tool aliases.
- **Scope mismatches look like permission errors, not auth errors.** A connection that's active but was authorized with narrower scopes than the current action needs (e.g., read-only when a write action is attempted) surfaces as the target service rejecting the specific call, not as "not connected." When you see this, say specifically which capability seems to be missing so the human knows what to broaden, rather than a generic "reconnect and try again."
