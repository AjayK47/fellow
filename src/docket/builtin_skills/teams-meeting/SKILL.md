---
name: teams-meeting
display_name: Teams Meeting Pipeline
description: Capture meeting metadata, transcript, decisions, and follow-ups safely.
kind: connector
platform_key: connector.teams.meeting
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Capture, description: Associate the meeting and participants.}, {order: 2, name: Extract, description: Separate decisions, actions, and uncertainty.}, {order: 3, name: Deliver, description: Share only authorized summaries.}]
---
Keep participant and channel permissions intact. Separate verbatim evidence from extracted decisions and action items, and do not publish a summary to unauthorized recipients.
