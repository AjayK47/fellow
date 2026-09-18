---
name: slack-communication
display_name: Write for Slack
description: Compose clear, concise responses for Slack channels and direct messages.
version: "1.0.0"
license: Apache-2.0
origin:
  project: toda
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: destination.slack.communication
activation:
  mode: mandatory
  destination_types: [slack_channel, private_channel, dm]
required_capabilities: {}
warnings:
  - Never expose private evidence into a public channel.
  - Preserve factual claims and place sources next to the claims they support.
steps:
  - order: 1
    name: Lead with the answer
    description: Answer directly, then add only the context needed to act.
  - order: 2
    name: Use native Slack formatting
    description: Use Slack mrkdwn and labeled angle-bracket links, not Markdown syntax.
  - order: 3
    name: Make it scannable
    description: Keep paragraphs short, use bullets or Block Kit fields instead of tables, and know when to switch to a document artifact.
---
This is the practical companion to Slack is the only delivery channel Toda
has, and to the system's own standing rule to keep Slack responses concise,
concrete, and action-oriented. That rule says *what*; this skill covers
*how* -- when plain text is enough versus when to reach for a card, and the
point at which a reply should become a document instead of a wall of text.

## Where the reply goes

Toda always replies within the same conversation -- public channel,
private channel, or DM -- that the request arrived in; there's no tool to
redirect a reply into a different channel or a separate thread. That means
the audience for a reply is exactly whoever can already see that
conversation -- never paste evidence gathered under a private or restricted
scope into a public channel just because the question came from there. If a
finding is only appropriate for a narrower audience than the current
channel, say that plainly and hold back the sensitive part rather than
posting it anyway.

## How long is too long

A Slack message is for the answer and the reasoning a reader needs right
now, not the full investigation trail. As a rough guide: if the content
would run past a few short paragraphs, needs a table, needs more than one
level of nested structure, or is meant to be saved and referenced later,
stop and produce a document artifact instead (`pdf_create` by default,
`docx_create` for something editable, `html_create` for anything visually
rich). Send the artifact with `slack_send_artifact` and put a short summary
plus the recommendation directly in the Slack message -- the message is the
headline, the artifact is the detail, never the reverse.

## Sources and citations

Keep a citation next to the specific claim it supports rather than
collecting all sources into an unlabeled list at the end of a short message
-- in a short reply, the reader can't tell which source backs which
sentence otherwise. For a longer artifact, a source list at the end is
fine.
