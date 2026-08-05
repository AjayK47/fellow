---
name: github-pr-workflow
display_name: GitHub Pull Request Workflow
description: Prepare and manage a pull request through bounded, reviewable steps using workspace inspection plus the Composio GitHub connector.
kind: connector
platform_key: connector.github.pr_workflow
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio, workspace.read]}
steps: [{order: 1, name: Prepare, description: Inspect the actual workspace diff and existing PR/commit state before writing anything.}, {order: 2, name: Review, description: Run whatever checks are available and surface risk plainly, including what you could not verify.}, {order: 3, name: Deliver, description: Create, update, or merge only after explicit approval, and confirm the result afterward.}]
warnings: ["There is no git or gh CLI here — branch creation, commits, and pushes are not something this skill can do directly; they either already happened in a coding specialist's workspace or need to be described to the human as a manual step.", "Creating a PR, merging, requesting reviewers, and posting a PR comment are all real, externally visible actions on someone else's repository — never fire one without first stating exactly what it will do.", "Merging is irreversible in the sense that matters (history, deployed code, notifications) even though GitHub allows reverts — always confirm CI status and reviewer approval before proposing a merge, and never merge without explicit human sign-off.", "A workspace_diff or workspace_read view of files is not the same as the actual GitHub PR diff — always cross-check against the PR's real file list from GitHub before describing scope in a PR description.", "Auto-merge and required-approval settings vary per repository — don't assume a PR can merge just because checks are green; confirm branch protection requirements via the connector before promising a merge will succeed."]
---
Docket has no shell access to run `git` or `gh`. This skill covers two different kinds of work that only look similar:

- **Local inspection** — reading whatever code changes already exist in the current run's workspace (written there by `workspace_write`/`workspace_patch`, or produced by a coding specialist) using `workspace_list`, `workspace_read`, `workspace_search`, and `workspace_diff`. This is how you build an accurate PR description and reason about risk.
- **GitHub-side actions** — everything that actually touches the repository on GitHub (checking existing PRs, opening one, checking CI, merging) happens exclusively through Composio, via `COMPOSIO_EXECUTE` and the curated `github_get_pull_request` / `github_list_commits` shortcuts. Docket cannot `git push` — a PR only exists once the branch and commits are already on GitHub, which in this harness comes either from a human, or from a Pi child Task (`delegate_tasks`) that has real shell and git access. This skill's job is to prepare, review, and manage that PR through the API, not to produce the commits.

## Finding the right GitHub action

`github_get_pull_request` (`GITHUB_GET_A_PULL_REQUEST`) and `github_list_commits` (`GITHUB_LIST_COMMITS`) are registered curated shortcuts — reach for them first for reading PR metadata and commit history. For everything else (listing changed files on a PR, opening a new PR, requesting reviewers, checking CI/check-run status, merging, enabling auto-merge, closing a PR), describe the use case to `COMPOSIO_SEARCH_TOOLS` — e.g. "list files changed in a GitHub pull request", "open a pull request from one branch to another", "request reviewers on a GitHub pull request", "get combined CI status for a commit", "merge a GitHub pull request with squash" — then confirm the exact arguments with `COMPOSIO_GET_TOOL_SCHEMAS` before calling `COMPOSIO_EXECUTE`.

## Workflow

### 1. Prepare

- If you (or a coding specialist working in the same run) made changes in the shared workspace, use `workspace_list` to see what changed and `workspace_read` / `workspace_diff` to look at the actual content — this is your source of truth for "what does this change do," not assumptions from the original task description.
- Confirm the target branch and base branch. If a PR already exists for this branch, use `github_get_pull_request` (or a discovered "list pull requests" action filtered by branch) to check before assuming you need to open a new one.
- Write a PR description grounded in what you actually read: a summary of the change, why it's needed, and a test plan reflecting what was actually verified — not a generic template filled with placeholders.

### 2. Review

- Pull the PR's real file list and diff from GitHub (a discovered "list pull request files" or "get pull request diff" action) rather than relying solely on the local workspace view — the workspace may contain files that were never staged, or may be missing context (e.g., a generated lockfile) that GitHub's diff will show.
- Check CI/check-run status via a discovered action (e.g. combined status or check-runs for the head commit). If checks haven't finished, say so plainly rather than presenting a stale or partial result as final.
- Surface risk explicitly: anything that touches auth, data migrations, external APIs, or deletes code should be called out in your summary before you propose merging.
- If you cannot verify something (no test suite ran, CI is still pending, you couldn't confirm branch protection rules), say exactly that — don't imply confidence you don't have.

### 3. Deliver

- **Opening a PR**, **requesting reviewers**, **posting a PR comment**, and **merging** are all real external writes. Describe the exact action (title, body, base/head branches, or for merge: which method — merge, squash, rebase) before calling `COMPOSIO_EXECUTE`, and only proceed once a human has actually approved it, not merely acknowledged your plan.
- After executing, re-fetch the PR (or read the action's response) and confirm the outcome — new PR number and URL, or the merge commit SHA — rather than assuming success from a 200-style response alone.
- If a merge requires reviews or checks the connector reports as not yet satisfied, report that back instead of retrying the merge call hoping it resolves itself.

## Gotchas specific to how Docket runs this

- **No git, no push.** If the task requires making code changes and you have no coding specialist available in this run, say so directly — this skill can describe, review, and manage PRs, but it cannot create the underlying commits or branch by itself.
- **Approval gate.** `github_get_pull_request` and `github_list_commits` are read-only and execute immediately. Any GitHub write reached through `COMPOSIO_EXECUTE` — opening a PR, commenting, requesting reviewers, merging — is fully audited, and a slug matching delete/remove-style destructive actions (e.g. closing without merging, in some flows) would pause for human approval; ordinary create/comment/merge actions on GitHub are not on that forced-approval list by default, which makes it more important that you've already stated the exact action and gotten explicit human sign-off in conversation before calling it.
- **CI status can lag.** A just-pushed commit's checks may not have started yet when you query status — report "pending" honestly rather than treating an empty check-run list as "no CI configured."
- **Multiple connected GitHub accounts** on the same organization require passing `connected_account_id` explicitly on `COMPOSIO_EXECUTE`, the same as any other GitHub connector skill.
- **Workspace and repository are not the same filesystem.** The run's workspace (`workspace_read`/`workspace_write`) is Docket's own sandboxed scratch space, not a checkout of the actual GitHub repository — never assume a file you can read via `workspace_read` is what's currently on the PR's branch without cross-checking GitHub directly.
