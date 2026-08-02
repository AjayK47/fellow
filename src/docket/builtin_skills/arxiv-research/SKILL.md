---
name: arxiv-research
display_name: arXiv Research
description: Find, inspect, and compare arXiv papers with bibliographic citations.
kind: builtin
platform_key: builtin.hermes.arxiv_research
activation: {mode: optional}
required_capabilities: {any_of: [web.search, web.fetch]}
steps: [{order: 1, name: Search, description: Find relevant papers and versions.}, {order: 2, name: Inspect, description: Read abstract, methods, and limitations.}, {order: 3, name: Compare, description: Synthesize with citations.}]
---
Use canonical arXiv identifiers and versions, distinguish abstracts from full-paper evidence, compare methods and limitations, and cite papers precisely.
