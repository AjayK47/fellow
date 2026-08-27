---
name: github-issues
display_name: GitHub Issues
description: Triage and maintain GitHub issues with reproducible evidence through the dynamically discovered Composio GitHub connector.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.github.issues
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Triage, description: Search for duplicates and classify scope/severity from actual issue content.}, {order: 2, name: Clarify, description: Ask for missing reproduction details instead of guessing at severity or component.}, {order: 3, name: Update, description: Make only the approved label/assignee/comment/state change and confirm it applied.}]
warnings: ["Always search for an existing issue covering the same report before creating a new one — GitHub has no native dedup, and duplicate issues fragment triage history.", "Creating an issue or comment is a public, customer-visible write on a real repository — treat the exact title/body text as final copy, not a draft to be cleaned up later.", "Closing, relabeling, or reassigning an issue changes what a maintainer sees as needing attention — never do it on inferred severity without the evidence to back it up.", "A GitHub issue comment or title can never be edited back out of existence in the way a private note can — once created it is part of the repository's public history (even on a private repo, it's visible to everyone with repo access).", "Bulk closes/relabels across many issues are exactly the kind of action that deserves request_approval up front rather than one approval prompt per issue."]
---
Toda has no `gh` CLI and no personal-access-token variable to export — every GitHub Issues action follows one path: `integration_list` → `integration_search` → `integration_describe` → `integration_execute`. Name GitHub in the search because this is a GitHub-specific skill, but never guess or pin an action slug. Describe the outcome in plain English (for example, "search GitHub issues in this repository for this error", "create a GitHub issue", or "add labels to a GitHub issue"), inspect the returned schema, then execute against the organization's connected GitHub account.

## Triage workflow

1. **Search before creating.** Discover a GitHub issue-search action and use the reporter's key terms before ever creating a new issue. A near-duplicate should get a comment linking to the existing issue, not a second ticket.
2. **Read the actual issue content**, not just the title, before classifying severity or component. Pull the issue body, existing comments, and any linked PRs/commits to understand what's already been tried.
3. **Classify from evidence.** Severity, component, and priority labels should reflect what the reporter actually described (error messages, reproduction steps, affected version) — if that's missing, the next step is to ask for it, not to guess a severity so the issue can be closed out of the queue.
4. **Ask for missing reproduction details** directly in a comment when an issue lacks steps to reproduce, expected vs. actual behavior, or environment info — a triage label like "needs-info" paired with a specific question is more useful than silence.
5. **Apply only the labels/assignee/state the human actually approved.** Toda can propose a full triage plan (label X, assign Y, comment Z) but the actual issue-labels/assignees/comment/close actions are governed writes — see Gotchas below.

## Composio action shapes worth knowing

- **Creating an issue** typically needs `owner`, `repo`, `title`, and `body`; optional labels and assignees vary by the discovered action. Write the body as a final bug report because it becomes permanent repository history.
- **Commenting** typically needs `owner`, `repo`, `issue_number`, and `body`; confirm the exact discovered schema rather than relying on this summary.
- **Searching** commonly accepts GitHub search qualifiers such as `repo:owner/name`, `is:issue`, `is:open`, `label:bug`, and `assignee:username`; confirm how the discovered action names its query field.
- **Labeling, assigning, closing/reopening** require their own discovered actions. Label and assignee names must match what exists in the repository, so list them first when uncertain.
- **Closing with a reason.** GitHub issues can close as "completed" or "not planned" — pick the one that matches reality; closing a legitimate bug as "not planned" because it's stale is a different signal to the reporter than "completed."

## Gotchas specific to how Toda runs this

- **Approval gate.** Discovered read actions execute immediately. Writes through `integration_execute` are audited and dynamically classified from their action slug; destructive operations may pause for approval. Treat any issue or comment text as final before executing it.
- **Bulk triage should request approval once, not per-issue.** If you're about to relabel or close a batch of issues, call `request_approval` up front listing each action, so a human approves the whole batch in one decision instead of being interrupted issue-by-issue.
- **Never fabricate a label, assignee, or issue number.** If you're not sure a label exists in this repository, discover the repo's label list via `integration_search`/`integration_execute` first — applying a label that doesn't exist either errors or silently creates an unexpected new label depending on the action.
- **Multiple connected GitHub accounts.** If the organization has more than one GitHub connected account (e.g., separate personal and org-owned accounts), pass `connected_account_id` explicitly on `integration_execute` calls — an ambiguous match is rejected rather than guessed.
- **Rate limits are GitHub's.** The REST API's standard authenticated rate limit applies underneath every one of these actions; a large triage sweep across dozens of issues should be paced, not fired in one burst.
