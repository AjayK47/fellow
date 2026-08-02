---
name: deep-research
display_name: Deep Research
description: Research questions using multiple authoritative sources and explicit citations.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: builtin.hermes.deep_research
activation:
  mode: optional
required_capabilities:
  any_of: [web.search, web.fetch]
warnings:
  - Do not present an uncited or weakly supported claim as fact.
steps:
  - order: 1
    name: Define the question
    description: Clarify scope, date sensitivity, and what evidence is needed.
  - order: 2
    name: Search and fetch
    description: Start broad, then inspect primary and authoritative sources.
  - order: 3
    name: Synthesize
    description: Cross-check claims, state uncertainty, and cite sources inline.
---
Define the question and its time boundary before searching. Search broadly, then
fetch primary or authoritative sources. Cross-check important claims, distinguish
facts from inference, and put a source link beside each material claim. If the
evidence conflicts or is incomplete, say so instead of inventing certainty.
