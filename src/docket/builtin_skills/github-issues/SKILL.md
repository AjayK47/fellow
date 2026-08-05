---
name: github-issues
display_name: GitHub Issues
description: Triage and maintain GitHub issues with reproducible evidence through Docket's curated GitHub shortcuts and the broader Composio GitHub connector.
kind: connector
platform_key: connector.github.issues
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Triage, description: Search for duplicates and classify scope/severity from actual issue content.}, {order: 2, name: Clarify, description: Ask for missing reproduction details instead of guessing at severity or component.}, {order: 3, name: Update, description: Make only the approved label/assignee/comment/state change and confirm it applied.}]
warnings: ["Always search for an existing issue covering the same report before creating a new one — GitHub has no native dedup, and duplicate issues fragment triage history.", "github_create_issue and github_add_comment are public, customer-visible writes on a real repository — treat the exact title/body text as final copy, not a draft to be cleaned up later.", "Closing, relabeling, or reassigning an issue changes what a maintainer sees as needing attention — never do it on inferred severity without the evidence to back it up.", "A GitHub issue comment or title can never be edited back out of existence in the way a private note can — once created it is part of the repository's public history (even on a private repo, it's visible to everyone with repo access).", "Bulk closes/relabels across many issues are exactly the kind of action that deserves request_batch_approval up front rather than one approval prompt per issue."]
---
Docket has no `gh` CLI and no personal-access-token variable to export — every GitHub Issues action is a Composio action. Two ways to reach it:

1. **Curated shortcuts** — `github_search_issues`, `github_create_issue`, `github_add_comment` are registered directly as tools (thin wrappers over fixed Composio slugs `GITHUB_SEARCH_ISSUES`, `GITHUB_CREATE_ISSUE`, `GITHUB_CREATE_AN_ISSUE_COMMENT`). Use these first for the common cases — they take a plain `arguments` object matching that action's schema, no slug lookup needed.
2. **`COMPOSIO_SEARCH_TOOLS` → `COMPOSIO_GET_TOOL_SCHEMAS` → `COMPOSIO_EXECUTE`** for anything the shortcuts don't cover: labeling, assigning, closing/reopening, listing labels, converting an issue to a discussion, linking an issue to a PR, bulk operations. Describe the use case in plain English to `COMPOSIO_SEARCH_TOOLS` (e.g. "add labels to a GitHub issue", "close a GitHub issue as not planned", "list open issues with a given label"), inspect the schema it returns, then execute.

Both paths ultimately run through the same Composio-backed GitHub connected account — there is no separate credential or auth step for the shortcuts versus the general path.

## Triage workflow

1. **Search before creating.** Use `github_search_issues` (or a `COMPOSIO_SEARCH_TOOLS`-discovered search action) with the reporter's key terms before ever creating a new issue. A near-duplicate should get a comment linking to the existing issue, not a second ticket.
2. **Read the actual issue content**, not just the title, before classifying severity or component. Pull the issue body, existing comments, and any linked PRs/commits to understand what's already been tried.
3. **Classify from evidence.** Severity, component, and priority labels should reflect what the reporter actually described (error messages, reproduction steps, affected version) — if that's missing, the next step is to ask for it, not to guess a severity so the issue can be closed out of the queue.
4. **Ask for missing reproduction details** directly in a comment when an issue lacks steps to reproduce, expected vs. actual behavior, or environment info — a triage label like "needs-info" paired with a specific question is more useful than silence.
5. **Apply only the labels/assignee/state the human actually approved.** Docket can propose a full triage plan (label X, assign Y, comment Z) but the actual issue-labels/assignees/comment/close actions are governed writes — see Gotchas below.

## Composio action shapes worth knowing

- **Creating an issue** (`github_create_issue` / `GITHUB_CREATE_ISSUE`) needs `owner`, `repo`, `title`, and typically `body`; optional `labels` and `assignees` arrays. Write the body the way you would a real bug report — description, reproduction steps, expected vs. actual behavior — since it becomes the issue's permanent record.
- **Commenting** (`github_add_comment` / `GITHUB_CREATE_AN_ISSUE_COMMENT`) needs `owner`, `repo`, `issue_number` (GitHub uses the same numbering for issues and PRs in this endpoint), and `body`.
- **Searching** (`github_search_issues` / `GITHUB_SEARCH_ISSUES`) accepts GitHub's own search-qualifier syntax (`repo:owner/name`, `is:issue`, `is:open`, `label:bug`, `assignee:username`) inside the query string — build the qualifier string the same way you'd type it into GitHub's own search box.
- **Labeling, assigning, closing/reopening** are not in the curated shortcut list — discover the exact slug via `COMPOSIO_SEARCH_TOOLS` (typical needs: "add labels to an issue", "assign a user to an issue", "close an issue with a reason", "list labels in a repository") and confirm required fields with `COMPOSIO_GET_TOOL_SCHEMAS` before calling `COMPOSIO_EXECUTE` — label/assignee names must match exactly what exists in the target repository, so list them first if you're not certain of the spelling.
- **Closing with a reason.** GitHub issues can close as "completed" or "not planned" — pick the one that matches reality; closing a legitimate bug as "not planned" because it's stale is a different signal to the reporter than "completed."

## Gotchas specific to how Docket runs this

- **Approval gate.** `github_search_issues` is read-only and executes immediately. `github_create_issue` and `github_add_comment` are registered as writes but are not on the destructive/financial/external-comms keyword list that forces approval, so they typically execute directly — they are still fully audited, which matters because these are real, public, permanent writes on someone else's repository. Treat the text you're about to post as final before calling the tool, not as a draft you'll fix after the fact.
- **Bulk triage should request approval once, not per-issue.** If you're about to relabel or close a batch of issues, call `request_batch_approval` up front listing each action, so a human approves the whole batch in one decision instead of being interrupted issue-by-issue.
- **Never fabricate a label, assignee, or issue number.** If you're not sure a label exists in this repository, discover the repo's label list via `COMPOSIO_SEARCH_TOOLS`/`COMPOSIO_EXECUTE` first — applying a label that doesn't exist either errors or silently creates an unexpected new label depending on the action.
- **Multiple connected GitHub accounts.** If the organization has more than one GitHub connected account (e.g., separate personal and org-owned accounts), pass `connected_account_id` explicitly on `COMPOSIO_EXECUTE` calls — an ambiguous match is rejected rather than guessed.
- **Rate limits are GitHub's.** The REST API's standard authenticated rate limit applies underneath every one of these actions; a large triage sweep across dozens of issues should be paced, not fired in one burst.
