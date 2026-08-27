---
name: google-meet
display_name: Google Meet
description: Process authorized Meet recordings and transcripts into useful follow-ups.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.google.meet
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Resolve, description: Confirm meeting and access.}, {order: 2, name: Summarize, description: Extract decisions and actions.}, {order: 3, name: Share, description: Respect audience and approvals.}]
warnings:
  - A meeting summary must only reach people who had access to the original
    meeting -- never post it to a broader channel, DM, or recipient list
    than the source meeting's own participant boundary.
  - Google Meet transcripts and recordings are not available the instant a
    meeting ends -- generation can take several minutes; an empty result
    right after a meeting ends is a timing gap, not proof nothing exists.
  - Action items and decisions you extract are your own inference from the
    transcript, not verbatim quotes -- keep the two clearly separated.
---
Toda has no dedicated Google Meet bot and does not join a call live --
there is no real-time capture here. Every interaction with a Meet meeting
(finding the event, reading its attendee list, locating a generated
transcript or recording) is a Composio action against the organization's
connected Google account, run through `integration_execute`, on artifacts
Google itself already produced after the meeting ended.

## Finding the right action

There's no curated shortcut for Meet specifically -- discover every time:

1. `integration_search` with a plain-English `use_case`, e.g. "list
   upcoming Google Calendar events with a Meet link", "get attendees for a
   Google Calendar event", "find the transcript document for a Google Meet
   meeting", "search recent files in Google Drive", "get a Google Doc's
   content". Meet's transcript and recording artifacts are typically
   generated into the organizer's Google Drive as a Google Doc (transcript)
   and a video file (recording), not served through a dedicated "get Meet
   transcript" endpoint -- so the actual retrieval is usually a Calendar
   lookup (to resolve the meeting and its time window) followed by a Drive
   search scoped to that time window and meeting title.
2. `integration_describe` on whatever slugs come back, since the exact
   field names for meeting ID, event ID, and Drive file ID vary by action.
3. `integration_execute` with `{tool_slug, arguments}`, passing
   `connected_account_id` explicitly if more than one Google account is
   connected for this organization.

## Resolve the meeting before reading anything

Identify the specific Calendar event the user means -- by title, time, or
an explicit link -- before searching for its transcript or recording.
Don't assume "the meeting" means the most recent one on the calendar
without confirming, especially for an organization with several meetings a
day. Pull the event's attendee list from Calendar alongside the transcript
search; who was actually invited (and who actually joined, if that
distinction is available in the source data) defines the permission
boundary for anything produced from this meeting later.

If the transcript or recording isn't found yet, say so plainly rather than
concluding it doesn't exist -- Meet's auto-generated transcript and
recording can take several minutes to land in Drive after the call ends,
and search may simply be running before generation finished.

## Summarize with evidence and inference kept separate

Read the transcript document in full before summarizing -- decisions often
get made in passing mid-discussion, not only in a closing recap. Keep two
categories visibly distinct in the output: a **verbatim** point (a direct
quote or close paraphrase attributed to a specific speaker, when the
transcript actually attributes speakers) and **your own extraction** (a
decision, action item, or owner you inferred from the discussion). Only
assign an action item to a named owner when the transcript actually
supports it; if ownership was left ambiguous in the meeting, say that
instead of guessing the likeliest person. If two participants appeared to
disagree and the transcript never resolves it, report the disagreement
rather than picking a side to report as "the decision."

## Share only within the original audience

Never post a meeting summary to a channel, DM, or recipient list broader
than the meeting's own attendee boundary -- this holds even if the summary
itself seems harmless, since meeting content routinely touches
compensation, personnel matters, unreleased plans, or other restricted
topics. State the source explicitly when delivering (which meeting, when,
who was invited) so the recipient can judge the summary's scope themselves.
Posting the summary anywhere, or sharing the Drive transcript/recording
more broadly, is a write action through `integration_execute` like any other --
confirm the destination and content before calling it, and remember that an
externally-facing or otherwise gated destination still pauses for the same
human approval any other write to that surface would need.

## Gotchas specific to how Toda runs this

- **No live capture.** This skill only ever processes what Google already
  generated after the call ended -- there's no way to join, listen to, or
  summarize a meeting that's currently in progress.
- **Transcript location varies by workspace configuration.** Some
  organizations configure Meet to save transcripts as a Doc in a specific
  shared drive or folder rather than the organizer's own Drive root --
  if a straightforward search doesn't find it, ask where the organization
  keeps generated transcripts rather than concluding none was made.
- **Attendee list vs. actual participants can differ.** A Calendar invite
  list is who was invited, not necessarily who joined or stayed for the
  whole call -- if the actual participant list matters for the permission
  boundary and isn't otherwise available, treat the invite list as the
  best available proxy and say so.
- **Multiple connected Google accounts** need an explicit
  `connected_account_id` on `integration_execute`, same as any other connector.
