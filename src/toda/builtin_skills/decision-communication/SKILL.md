---
name: decision-communication
display_name: Decision Communication
description: Communicate decisions with a direct recommendation, tradeoffs, and next steps.
version: "1.0.0"
license: Apache-2.0
origin:
  project: toda
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.decision_communication
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Mark any action that requires human approval explicitly -- don't imply
    it already happened or will happen automatically.
  - Never bury the actual recommendation inside a paragraph of context; it
    belongs in the first sentence.
steps:
  - order: 1
    name: State the decision
    description: Lead with the recommended option in the first sentence, not the last.
  - order: 2
    name: Explain tradeoffs
    description: Give the reasoning and evidence, compare only materially different alternatives.
  - order: 3
    name: Surface caveats, then sources
    description: State real uncertainty and remaining risk, then cite the evidence behind the recommendation.
---
Use this whenever the answer is a recommendation or a decision, not a plain
fact: "should we do X or Y," "what do you think we should do about Z," or the
conclusion of an investigation that implies a choice. The goal is a reader
who can act on the first sentence and only reads further if they want the
reasoning.

## Lead with the bottom line

The very first sentence should be the actual recommendation or answer --
not "I looked into this and here's what I found," not a restatement of the
question, not a paragraph of scene-setting. If a reader stopped after the
first sentence, they should already know what you think should happen. This
is the single most common failure mode to avoid: burying the actual
recommendation in paragraph three after a long windup.

Bad: "I did some digging into our retry logic and looked at a few options
for handling the intermittent failures we've been seeing, and after
weighing the tradeoffs, here's my thinking:"

Good: "Use exponential backoff with jitter in a shared retry utility. Here's
why:"

## Then the reasoning

After the headline, give the material reasoning: what evidence supports
this recommendation, and why it beats the realistic alternatives. Only
mention an alternative if it would change the reader's decision if they
picked it instead -- don't list every option considered along the way just
to demonstrate thoroughness. Two or three sentences of "why" is usually
enough; if it takes a full page to justify, that's a sign the finding
belongs in a document artifact with this Slack message as its summary, not
the other way around.

## Don't bury caveats -- but don't lead with them either

State real uncertainty and risk plainly, after the recommendation and
reasoning, not folded into a hedge in the first sentence and not omitted
entirely. "I'm fairly confident in this, but haven't checked X" is more
useful than false certainty or a recommendation so hedged it reads as no
recommendation at all. If an action requires human approval before it can
happen (per the system's gating rules for a destructive, financial, or
external-facing action), say that explicitly as part of the next steps,
not as an afterthought the reader has to infer.

## Sources last, but present

Put citations and evidence sources after the reasoning and caveats, not
scattered awkwardly through the headline sentence. A source belongs next to
the specific claim it supports when the message is short enough for that to
read cleanly; for a longer writeup, a short source list at the end is fine.
Never state a company-specific claim without a source or memory ID behind
it, per the system's citation requirement.

## What NOT to bury

Three things people most often bury and shouldn't: the actual
recommendation (put it first), an action that needs approval before it
takes effect (name it explicitly), and a caveat that would change whether
the reader trusts the recommendation (state it, don't hope it goes
unnoticed). Everything else -- the full investigation trail, every
alternative considered, minor details -- can be compressed or moved to a
linked artifact.

## Format

For Slack delivery, follow the slack-communication skill's formatting rules
(mrkdwn, no headings or tables, short paragraphs). For a genuinely complex
decision with several real alternatives, the one-three-one skill's stricter
one-headline/three-facts/one-ask format is a good fit; reach for that when
the structure itself needs to be that rigid, and use this skill's looser
lead-with-the-answer discipline otherwise.
