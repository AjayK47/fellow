---
name: slack-communication
display_name: Write for Slack
description: Compose clear, concise responses for Slack channels and direct messages.
version: "1.0.0"
license: Apache-2.0
origin:
  project: docket
  adapted_from: hermes-agent
  source_commit: 4b60979
kind: builtin
platform_key: destination.slack.communication
activation:
  mode: mandatory
  destination_types: [slack_channel, private_channel, dm]
required_capabilities: {}
response_validation:
  enabled: true
  instructions: Use Slack mrkdwn, labeled <URL|text> links, short paragraphs, and no Markdown headings, tables, **bold**, or [label](URL) links.
warnings:
  - Never expose private evidence into a public channel.
  - Preserve factual claims and place sources next to the claims they support.
steps:
  - order: 1
    name: Lead with the answer
    description: Answer directly, then add only the context needed to act.
  - order: 2
    name: Use native Slack formatting
    description: Use Slack mrkdwn and labeled angle-bracket links.
  - order: 3
    name: Make it scannable
    description: Keep paragraphs short and use bullets instead of tables.
---
When replying to Slack, write for a busy teammate reading on a phone. Lead with
the answer. Use Slack mrkdwn: *bold*, _italic_, ~strike~, `inline code`, and
triple-backtick code blocks. Use `<https://example.com|label>` for labeled links.
Do not emit Markdown headings, tables, `**bold**`, or `[label](url)` links. Keep
the response concise, preserve citations, and never disclose evidence that the
recipient is not authorized to see.
