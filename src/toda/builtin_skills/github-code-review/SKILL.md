---
name: github-code-review
display_name: GitHub Code Review
description: Review GitHub changes for correctness, security, and regression risk through the Composio GitHub connector.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.github.code_review
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Read context, description: Pull the PR's real metadata, file list, and diff from GitHub before forming any opinion.}, {order: 2, name: Check behavior, description: Work the review checklist against the actual diff and, where available, surrounding file content.}, {order: 3, name: Report, description: Give prioritized findings with file/line evidence, separated from open questions, before posting anything.}]
warnings: ["Never post a formal review (approve / request changes / inline comments) without having actually read the diff — a review based on the PR title and description alone is not a review.", "Inline review comments must reference the correct commit SHA (the PR's current head) and the correct side (new file vs. old file) or GitHub will reject them or attach them to the wrong line.", "Submitting a review is a single-shot action per review object in most Composio review actions — build the full list of inline comments first, then submit once, rather than trying to add comments to an already-submitted review.", "A review verdict (approve vs. request changes) is a real signal other humans will act on — don't default to approve just because nothing looks broken if you were unable to verify tests or CI at all.", "Findings phrased as questions ('is this intentional?') should be visibly separate from findings phrased as defects ('this will throw on empty input') — conflating the two makes a review harder to act on."]
---
Toda has no local checkout and no `git diff` to run — every piece of context for a code review comes from GitHub itself through Composio, and every comment/review you post is a real, permanent, visible action on that repository. There is no `gh` CLI shortcut either; discovery and execution both go through Composio's tool surface.

## Finding the right action

For every GitHub operation, describe the use case to `integration_search`, inspect the returned schema with `integration_describe`, and call `integration_execute`. Do not pin an action slug or expect a GitHub-specific tool alias:

- "get the list of files changed in a GitHub pull request" (with additions/deletions per file)
- "get the diff for a GitHub pull request"
- "get commit status / check runs for a GitHub commit" (CI results)
- "post an inline review comment on a specific file and line of a GitHub pull request"
- "submit a pull request review with a verdict" (approve / request changes / comment, optionally bundled with multiple inline comments in one call)
- "post a general comment on a GitHub pull request"

Prefer the bundled review-submission action (multiple inline comments plus one top-level verdict in a single call) over one-comment-at-a-time actions when you have several findings — it reads as one coherent review to the PR author instead of a scattered sequence of comments, and it's one approval event instead of several.

## Review workflow

### 1. Read context

- Discover and fetch the PR's metadata: title, description, author, base/head branches, current state.
- Fetch the actual changed-files list and diff via a discovered action — do not review from the title/description alone.
- Note the head commit SHA; you'll need it for any inline comment.
- If the PR is large, work file by file rather than trying to hold the entire diff in one pass.

### 2. Check behavior

Apply the same review lens regardless of language or repo, working from the real diff:

- **Correctness** — does the change do what it claims; are edge cases (empty input, nulls, concurrent access) handled; are error paths graceful?
- **Security** — no hardcoded secrets or credentials, input validation on anything user-facing, no injection-style patterns, auth/authz checks present where the change touches access control.
- **Code quality** — clear naming, no unnecessary complexity, no duplicated logic that should be consolidated, functions doing one thing.
- **Testing** — are new code paths covered; happy path and failure path both exercised?
- **Performance** — no obvious N+1 patterns or unnecessary blocking work introduced.
- **Documentation** — public API changes documented; non-obvious logic explained; README/docs updated if user-facing behavior changed.
- **CI** — pull check-run/status results for the head commit if a relevant action is available, and say plainly if you couldn't verify this (pending, no discovered action, or none configured) rather than silently skipping it.

### 3. Report

Structure findings by severity, and keep questions visibly separate from defects:

```
## Code Review Summary

### Critical
- path/to/file.py:45 — <concrete defect, why it matters>

### Warnings
- path/to/file.py:23 — <real but non-blocking issue>

### Suggestions
- path/to/file.py:8 — <optional improvement>

### Open questions
- <things you genuinely don't know the answer to — intent, requirements, etc.>

### Looks good
- <what's actually solid, briefly>
```

**Verdict rule:** approve only when there are no critical or warning-level findings and you were able to verify at least the diff itself (CI/tests too, if available). Request changes when any critical or warning issue should block merge. Use "comment" (no verdict) when you have observations but nothing blocking, or when you couldn't verify enough to commit to approve/request-changes.

## Gotchas specific to how Toda runs this

- **Everything you post is public-ish and permanent.** Inline comments, review verdicts, and summary comments are visible to everyone with repo access and cannot be un-posted the way a private note can be discarded. Draft the full review content in your own reasoning before calling the posting action.
- **Approval gate.** Reading PR metadata, diffs, and CI status is read-only and executes immediately. Posting a review, inline comments, or a summary comment are writes; they are fully audited and typically execute directly (they are not on the forced-approval destructive/financial/external-comms list), which puts the responsibility on you to only fire the posting call once you're confident in the content — there is no "undo" step afterward.
- **Match the commit SHA.** If the PR has been updated since you last fetched it (new commits pushed mid-review), re-fetch the head SHA before posting inline comments — stale SHAs cause the comment to fail or attach to the wrong diff view.
- **Don't conflate local review guidance with this connector.** Nothing here runs `git diff` or reads a local checkout — if a human is instead asking you to review code you already have open in the run's workspace (not a GitHub PR), that's local inspection via `read_file`/`file_diff`, not this skill's connector path.
- **Multiple connected GitHub accounts** on the same organization require passing `connected_account_id` explicitly to `integration_execute`.
