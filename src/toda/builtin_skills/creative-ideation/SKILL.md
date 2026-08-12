---
name: creative-ideation
display_name: Creative Ideation
description: Generate, compare, and shape novel ideas against explicit constraints.
version: "1.0.0"
license: Apache-2.0
origin:
  project: toda
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.creative_ideation
activation:
  mode: optional
required_capabilities: {}
warnings:
  - Refuse your own first three ideas -- they are almost always the most
    obvious, generic answer and not worth presenting as-is.
  - Do not stack every idea into a single undifferentiated list; group by
    the actual dimension that varies (approach, risk, effort), not just numbering.
steps:
  - order: 1
    name: Frame constraints
    description: Restate audience, goal, resources, and non-negotiables before generating anything.
  - order: 2
    name: Diverge
    description: Produce several genuinely different directions, not variations on one idea.
  - order: 3
    name: Converge
    description: Compare options honestly and recommend one grounded next step.
---
Use this whenever the request is open-ended and generative: "give me ideas
for X," "I'm stuck," "what should we build/write/try," "help me pick between
these," or "make this less obvious." It applies to any domain -- product
features, campaign angles, an automation to build, a way to phrase something,
a technical approach -- not just traditionally "creative" work.

## Constraint plus direction produces ideas, not a blank page

Before generating anything, name what's actually fixed: the audience, the
goal, real resource limits (time, headcount, budget), and any non-negotiable
requirement. A request with no constraint at all ("inspire me") needs one
invented and stated before ideas can be judged as good or bad against it.
Skipping this step is why unrouted brainstorming produces generic filler --
state the frame explicitly, even in one sentence, before diverging.

## Diverge before judging

Generate several ideas that differ in actual approach, not in surface
wording -- three versions of the same mechanism are one idea, not three.
Resist presenting the first three things that come to mind; they tend to be
the most statistically obvious answer to the prompt and read as generic to
anyone who has seen an AI brainstorm before. Push past them, especially on
well-trodden terrain (a new app idea, a marketing angle, a team process) --
those are exactly the categories where the obvious answer is the least
useful one.

## Be specific, not abstract

"A dashboard that tracks engagement" is filler; "a weekly Slack digest that
flags any channel whose message volume dropped more than 40% week-over-week"
is a direction someone can actually evaluate and build. Name real
mechanisms, real numbers, real first steps -- not a vague category of
solution. Specificity is what makes an idea gradable; abstraction just
defers the real thinking to whoever reads it.

## Converge honestly

After diverging, compare the ideas on the dimensions that actually matter for
this decision -- typically feasibility, expected impact, and risk or
reversibility. Recommend one, explain why it beats the alternatives on those
terms, and name the smallest concrete next step or experiment that would
validate it (a prototype, a one-week trial, a small pilot with a defined
group). At least one option in the set should be genuinely pursuable now,
with a real first step -- not every idea needs to be safe, but the set as a
whole shouldn't be all reach and no ground.

## Delivering the result

For a short exchange, present the ideas directly in Slack using the decision-
communication skill's structure (lead with the recommendation). For a larger
ideation session with many options worth preserving (a workshop output, a
longer options memo), use `pdf_create` or `docx_create` to produce a artifact
the user can share onward, and say so rather than pasting a wall of text into
Slack.
