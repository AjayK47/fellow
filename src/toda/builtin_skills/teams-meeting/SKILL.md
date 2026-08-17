---
name: teams-meeting
display_name: Teams Meeting Pipeline
description: Capture Microsoft Teams meeting metadata, transcript, decisions, and follow-ups through the Composio Microsoft Teams/Graph connector, without leaking permissions.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.teams.meeting
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Capture, description: Resolve the specific meeting and its actual participant list before reading anything.}, {order: 2, name: Extract, description: Separate verbatim transcript evidence from your own inferred decisions and action items.}, {order: 3, name: Deliver, description: Share the summary only with people who were actually authorized to see the source meeting.}]
warnings: ["A meeting summary or transcript excerpt must only reach people who had access to the original meeting or channel — never post it to a broader channel, DM, or recipient list than the source meeting's own participant/permission boundary.", "Microsoft Graph's transcript and recording artifacts are not available the instant a meeting ends — they can take several minutes to generate; an empty result immediately after a meeting ends is a timing gap, not proof no transcript exists.", "Meeting content often contains sensitive discussion (compensation, personnel, security incidents, unreleased plans) — treat every transcript as containing potentially restricted information until you've confirmed the destination is within its original permission boundary.", "Action items and decisions you extract are your own inference from the transcript, not verbatim quotes — always present them clearly separated from anything directly quoted, so a reader can tell what was actually said versus what you concluded.", "There is no CLI or background pipeline daemon in this harness — meeting ingestion happens only when you explicitly fetch it through a Composio action for a specific meeting; nothing runs on a schedule unless the organization has separately configured an automation for it."]
---
Toda has no `hermes teams-pipeline` CLI, no standing Microsoft Graph webhook subscription that Toda itself manages from inside a conversation, and no separate operator tool for this. Every Teams/Graph interaction — resolving a meeting, fetching its transcript or recording metadata, listing participants, posting a summary back into a channel — is a Composio action run through `COMPOSIO_EXECUTE` against the organization's connected Microsoft Teams (or broader Microsoft 365/Graph) account. If the organization wants meetings ingested automatically on a schedule rather than fetched on request, that's a separate automation/webhook setup outside this skill's scope — this skill covers what to do when you're asked, in the moment, to summarize or extract from a specific meeting.

## Finding the right action

There's no curated shortcut for Teams — discover every time:

1. `COMPOSIO_SEARCH_TOOLS` with a plain-English `use_case` — e.g. "list recent Microsoft Teams meetings", "get the transcript for a Teams meeting", "get participants of a Teams meeting", "get recording details for a Teams meeting", "post a message to a Microsoft Teams channel". It returns the matching Graph-backed action(s) for whatever the organization's connected account actually supports.
2. `COMPOSIO_GET_TOOL_SCHEMAS` on the returned slug(s) — meeting/transcript lookups typically need either a meeting ID or a join-URL-style reference; confirm which one the discovered action expects before asking the user for it.
3. `COMPOSIO_EXECUTE` with `{tool_slug, arguments}`, passing `connected_account_id` explicitly if more than one Microsoft account is connected.

## Workflow

### 1. Capture

- Resolve the specific meeting the user means — by meeting ID, join URL, or a time/title description you first look up — before fetching anything. Don't assume "the meeting" refers to the most recent one without confirming, especially if the organization has several meetings a day.
- Fetch the participant list alongside the transcript. Who was actually in the meeting defines the permission boundary for anything you produce from it later.
- If the transcript or recording isn't available yet, say so plainly — a meeting that ended minutes ago may not have a generated transcript yet; don't report "no transcript exists" as a permanent fact when it may just not be ready.

### 2. Extract

- Read the full transcript before summarizing — decisions and commitments are sometimes made in passing mid-discussion, not only in a clean closing recap.
- Keep two clearly separate categories in your output: **verbatim evidence** (a direct quote or close paraphrase clearly attributed to a speaker and moment) and **your own extraction** (a decision, action item, or owner you inferred from the discussion). A reader should never have to guess which is which.
- Assign action items to a specific named owner only when the transcript actually supports that — if ownership was ambiguous or left open in the meeting, say that explicitly rather than assigning it to whoever seems likeliest.
- Flag genuine uncertainty rather than smoothing it over — if two speakers seemed to disagree on a decision and the transcript doesn't resolve it, say that instead of picking one side to report as "the decision."

### 3. Deliver

- **Never widen the audience.** A summary of a meeting must go only to the meeting's own participants or an equally-restricted channel — posting it to a broader Teams channel, a Slack channel, or an email list that wasn't part of the original meeting's access is a permission violation, not a convenience. If you're unsure who had access to the source meeting, ask before delivering rather than guessing wide.
- **State the source explicitly** when delivering a summary — which meeting, when, who was present — so the recipient can judge the summary's scope and reliability themselves.
- **Posting the summary back into a Teams channel** (or any other destination) is a write action through `COMPOSIO_EXECUTE` like any other — confirm the exact destination and content before calling it.

## Gotchas specific to how Toda runs this

- **Approval gate.** Reading meeting metadata, participants, and transcripts is a read and executes immediately. Posting a summary into a channel or sending it elsewhere is a write; it's fully audited, and if the destination toolkit is one of Toda's external/customer-facing surfaces it may pause for approval — either way, the permission-boundary check above is your responsibility regardless of what the platform additionally gates.
- **Transcript timing, not pipeline failure.** An empty transcript result on a meeting that ended very recently is almost always a "not generated yet" timing gap. Wait a few minutes and retry before concluding transcription failed or wasn't enabled for that meeting.
- **No standing automation from inside a conversation.** If the user wants every meeting summarized automatically going forward rather than on request, that's a scheduled automation or webhook subscription configured outside a chat turn — this skill doesn't set that up for them; point them to whatever automation/admin surface the organization uses for recurring work.
- **Multiple connected Microsoft accounts** require explicit `connected_account_id` on `COMPOSIO_EXECUTE`, same as any other connector.
- **Meeting content sensitivity compounds normal privacy rules.** Treat every transcript as potentially containing information (personnel matters, unreleased plans, security incidents) that deserves at least as much caution as any other permission-scoped company data Toda handles — the meeting-specific participant boundary is in addition to, not instead of, the organization's normal access rules.
