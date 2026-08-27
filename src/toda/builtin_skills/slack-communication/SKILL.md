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
response_validation:
  enabled: true
  instructions: Use Slack mrkdwn, labeled <URL|text> links, short paragraphs, and no Markdown headings, tables, **bold**, or [label](URL) links.
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
*how* -- the actual mrkdwn syntax, when plain text is enough versus when to
reach for Block Kit, and the point at which a reply should become a document
instead of a wall of Slack text.

## Slack mrkdwn, not Markdown

Slack does not render standard Markdown. Use:

- `*bold*` (single asterisks), not `**bold**`
- `_italic_`, `~strikethrough~`, `` `inline code` ``
- Triple-backtick fenced blocks for multi-line code
- `<https://example.com|link label>` for a labeled link -- `[label](url)`
  renders literally as bracket-and-parenthesis text, not a link
- A plain `-` or `•` for bullets; there is no native table syntax, so a
  comparison that needs columns either becomes a short bulleted list per
  item or moves into a document artifact
- No `#`/`##` headings -- Slack doesn't render them as headings, they just
  show up as literal hash marks. Use a bolded short lead-in line instead if
  a section needs a label.

## Plain text vs. Block Kit

Plain mrkdwn text is enough for a short, single-fact answer or a simple
recommendation. Reach for `slack_send_message` with Block Kit blocks
specifically when the structure itself is part of the answer -- a
multi-step outcome, a checklist of several things checked or fixed, or a
before/after comparison. A `section` block with `fields` renders as a clean
two-column layout without needing a Markdown table:

```
{"type": "section", "fields": [
  {"type": "mrkdwn", "text": "*Checked:*\n5 endpoints"},
  {"type": "mrkdwn", "text": "*Failing:*\n1 (auth timeout)"}
]}
```

Use `ask_user` with its `choices` array (2-10 short options) when
the next step is genuinely a pick from a known set -- Slack renders that as
buttons or a menu instead of asking the user to type a free-text reply.
Reserve it for a real blocker, not a way to avoid making a reasonable
default call yourself.

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

## Tone

Write for a busy teammate reading on a phone: answer first, minimum context
needed to act, no chatbot filler ("Great question!", "I hope this helps!").
If the draft reads stiff or generically AI-written before sending, the
humanizer skill is the right pass to fix that without changing the
substance.
