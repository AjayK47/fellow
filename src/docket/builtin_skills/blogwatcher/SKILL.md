---
name: blogwatcher
display_name: Blog Watcher
description: Monitor selected blogs for meaningful new posts and summarize changes.
kind: builtin
platform_key: builtin.hermes.blogwatcher
activation: {mode: optional}
required_capabilities: {any_of: [web.search, web.fetch]}
steps: [{order: 1, name: Check, description: Inspect configured sources and dates.}, {order: 2, name: Filter, description: Remove duplicates and irrelevant posts.}, {order: 3, name: Digest, description: Summarize only meaningful changes.}]
---
Compare against the last known checkpoint, deduplicate posts, respect publication dates, and deliver a compact digest only when something relevant changed.
