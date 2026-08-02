---
name: youtube-content
display_name: YouTube Content
description: Research and summarize authorized or public YouTube content with timestamps.
kind: builtin
platform_key: builtin.hermes.youtube_content
activation: {mode: optional}
required_capabilities: {any_of: [web.search, web.fetch]}
steps: [{order: 1, name: Locate, description: Resolve the exact video and date.}, {order: 2, name: Extract, description: Use transcript or source evidence.}, {order: 3, name: Summarize, description: Cite timestamps and uncertainty.}]
---
Identify the exact video, channel, and publication date. Prefer a transcript or primary description, cite timestamps when available, and distinguish the speaker's claims from your inference.
