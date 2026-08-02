---
name: google-meet
display_name: Google Meet
description: Process authorized Meet recordings and transcripts into useful follow-ups.
kind: connector
platform_key: connector.google.meet
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Resolve, description: Confirm meeting and access.}, {order: 2, name: Summarize, description: Extract decisions and actions.}, {order: 3, name: Share, description: Respect audience and approvals.}]
---
Confirm the meeting, access scope, and participant audience. Preserve speaker uncertainty, separate decisions from discussion, and require approval before external delivery.
