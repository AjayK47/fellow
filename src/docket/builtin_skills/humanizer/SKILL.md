---
name: humanizer
display_name: Humanize Writing
description: Make generated writing direct, natural, specific, and appropriate for its audience.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.humanizer
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Do not remove necessary technical precision, facts, numbers, or hedges
    that reflect real uncertainty -- this is a style pass, not a content edit.
  - Match the requested tone; don't inject informality into a piece that
    needs to stay formal (a policy, a legal notice, an external announcement).
steps:
  - order: 1
    name: Remove filler
    description: Cut generic openings, repetition, empty hedging, and AI-typical phrasing.
  - order: 2
    name: Use concrete language
    description: Prefer active voice, specific verbs, real numbers, and direct claims.
  - order: 3
    name: Preserve intent
    description: Keep facts, constraints, citations, and the requested tone intact.
---
Apply this whenever a draft -- yours or the user's -- reads as generic
AI-generated prose: a Slack reply, a document artifact, a PR description, a
status update, or any other user-facing text. It's a style pass over
already-drafted text, not a research or fact-checking step; run it after
content is settled, and don't let it change what the text actually claims.

## The tell-tale patterns to remove

**Inflated significance.** Phrases like "stands as a testament to," "marks a
pivotal moment," "underscores its importance," "reflects broader trends" --
these puff up ordinary facts. State the fact plainly instead: what happened,
when, and why it matters concretely, not that it "represents a shift."

**Vague attribution.** "Industry reports suggest," "experts believe,"
"observers have noted" -- if there's a real source, name it; if there isn't
one, cut the sentence rather than dress it up with invented authority.

**Filler and hedging.** "In order to," "due to the fact that," "at this
point in time," "it is important to note that" all compress to a shorter,
plainer phrase ("to," "because," "now," or nothing at all). Excessive
hedging ("could potentially possibly") should collapse to one honest
qualifier, or none if the claim is actually solid.

**Copula avoidance.** "Serves as," "functions as," "stands as" dressing up a
plain "is." Say "is."

**Mechanical structure.** Rule-of-three lists ("innovation, inspiration, and
insight"), inline-bolded-header bullet lists ("**Performance:** Performance
has been strong"), a formulaic "Challenges and Future Outlook" section, and
negative parallelism ("It's not just X, it's Y") all read as templated.
Replace them with plain sentences that say the actual thing.

**Synonym cycling ("elegant variation").** Calling the same entity "the
protagonist," then "the main character," then "the central figure" in
consecutive sentences reads as an artifact of repetition-avoidance, not
style. Reuse the same term.

**Formatting tells.** Em-dash overuse, mechanical boldface on key phrases,
Title Case headings, emoji decoration on bullets, and curly quotes are all
easy to spot and easy to fix -- plain sentences, sentence-case headings, no
decorative emoji, straight quotes.

**Chatbot residue.** "I hope this helps!", "Great question!", "Let me know
if you'd like me to expand" -- these are conversational artifacts that
belong in a chat reply, not in the delivered content itself. Cut them from
anything meant to be read as a finished document.

**Signposting and rhetorical questions.** "Let's dive in," "here's what you
need to know," or posing a question only to answer it a beat later
("What makes a good API? It's predictability.") -- state the point
directly instead of narrating that you're about to make it.

## Add real voice, not just clean prose

Removing bad patterns produces prose that is merely correct, not readable.
Vary sentence length and rhythm on purpose. Let a considered opinion show
through ("this result is genuinely surprising") instead of flat neutral
reporting. Acknowledge real complexity or mixed evidence rather than
smoothing it into one clean conclusion. Use first person where it's honest
and fits the context. A little unevenness -- an aside, a shorter sentence
breaking up a long one -- reads as human; uniform, evenly-paced sentences
read as generated.

## If a voice sample is available

When the user has supplied their own prior writing to match, read it first
and note concretely how they write: typical sentence length, formality
level, how they open a paragraph, any recurring phrasing or punctuation
habits. Match those specifics in the rewrite rather than defaulting to a
generic "natural" voice -- if they write short sentences and casual word
choices, don't upgrade "stuff" to "elements."

## Process

1. Read the draft and identify which of the patterns above are present.
2. Rewrite the affected sections, preserving every fact, number, citation,
   and constraint exactly as they were.
3. Ask yourself plainly what still reads as obviously AI-generated, and do
   one more pass to fix it.
4. Present the final version. For a short reply, this is the direct answer;
   for a longer document artifact, re-apply document-tool guidance (e.g.
   `docx_create`/`pdf_create`) after the text is finalized, not before.

## What NOT to touch

Never soften a real hedge that reflects genuine uncertainty, never drop a
citation or source attribution to make a sentence read cleaner, and never
change a number, name, date, or claim while editing for style -- if the
fact-level content needs to change, that's a different task, not this one.
