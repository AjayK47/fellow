---
name: one-three-one
display_name: One-Three-One Updates
description: Structure status updates as one headline, three facts, and one ask.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.one_three_one
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Do not pad the update with history or context the reader already has --
    if there's truly nothing new, say that in the headline and stop.
steps:
  - order: 1
    name: Headline
    description: State the current status in one sentence.
  - order: 2
    name: Three facts
    description: Give the three most decision-relevant facts, not every fact available.
  - order: 3
    name: One ask
    description: State one clear decision or action needed, or say none is needed.
---
Use this format for a status update or check-in: "how's X going," a recurring
automation's report, a progress update on a multi-step task, or any moment
where the reader wants the current state fast, not a narrative of how you got
there. It's a narrower, more mechanical format than the decision-communication
skill -- reach for one-three-one when the ask is "what's the status," and for
decision-communication when the ask is "what should we do."

## The format

**One headline.** A single sentence stating the current status. This is the
sentence someone reads if they read nothing else -- make it carry the actual
news, not a placeholder like "here's an update."

**Three facts.** Pick the three facts that would change what the reader
does next, not the three most recent or most available facts. If there are
eight things that happened, that's a sign to compress harder, not to list
five extra bullets "for completeness." A fact here is a plain claim with a
number or a concrete outcome attached (e.g. "reconciliation found 3
discrepancies, 2 already fixed"), not a vague summary of activity.

**One ask.** State exactly one decision or action needed from the reader,
or say explicitly that none is needed right now. Don't bury an ask inside
one of the three facts, and don't list multiple asks -- if there are
genuinely several, pick the one that's actually blocking and mention the
others only as a brief aside.

## Fitting this into Slack

For a plain-text reply, three short lines or a tight paragraph works. For a
richer update (numbers worth scanning, a before/after), prefer
`slack_send_message` with Block Kit `fields` -- a short mrkdwn section for
the headline, then a fields section for the three facts -- per the system
prompt's guidance on richer Slack replies. Keep the ask as its own final
line so it isn't lost inside the formatting.

## On an automation-triggered run

When this update is being generated because a schedule fired rather than
because someone asked, the fact that the schedule fired is not itself news.
If nothing meaningful changed since the last check, the headline should say
that plainly ("no change since the last check") and the update can stop
there -- don't manufacture three facts out of routine, unchanged state just
to fill the format.

## Example

Headline: "The nightly data sync completed with one new failure."

Facts:
- 1,204 of 1,205 records synced successfully.
- The failed record (`order_8831`) failed on a schema mismatch, not a
  transient error.
- This is the second schema-mismatch failure this week from the same
  upstream field.

Ask: "Should I open a ticket with the upstream team, or is this a known,
already-tracked issue?"
