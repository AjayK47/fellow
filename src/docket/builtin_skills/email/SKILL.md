---
name: email
display_name: Email
description: Search, summarize, draft, and send email with recipient and privacy checks.
kind: connector
platform_key: connector.email
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Search, description: Find the relevant conversation.}, {order: 2, name: Draft, description: Preserve intent and recipients.}, {order: 3, name: Send, description: Require approval and verify delivery.}]
---
Treat email as a high-impact external write. Verify recipients, quoted context, attachments, and privacy before asking for approval; never send implicitly.
