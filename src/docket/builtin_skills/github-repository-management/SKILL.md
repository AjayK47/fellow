---
name: github-repository-management
display_name: GitHub Repository Management
description: Maintain repository metadata, settings, releases, and workflows with guarded writes through the Composio GitHub connector.
kind: connector
platform_key: connector.github.repository_management
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Inspect, description: Read the current repository state (settings, protection rules, workflows, releases) before proposing anything.}, {order: 2, name: Plan, description: Describe the exact intended mutation, including what it will NOT change.}, {order: 3, name: Apply, description: Make only the approved change and re-read the resource to confirm it took effect.}]
warnings: ["Never delete a repository, branch, tag, or release, change branch protection rules, or alter collaborator/team permissions without an explicit human approval for that specific action — these are exactly the class of write this connector treats as high-stakes.", "Repository visibility changes (private to public) can expose code, history, and secrets in commit history that were never meant to be public — confirm this is really intended before touching visibility.", "Branch protection and required-status-check settings affect every future PR on that branch, not just the one in front of you — read the current rule set fully before proposing a change to it.", "Re-running or cancelling a GitHub Actions workflow run affects a real CI/CD pipeline that may deploy code or touch external systems — treat it with the same care as a manual production action.", "GitHub Actions secrets are opaque once written — never attempt to read a secret's value back, and never construct a message that includes what looks like a secret's plaintext value."]
---
Docket has no `gh` CLI, no local git remote, and no direct GitHub token — every repository-management action (creating/forking a repo, editing settings, branch protection, releases, secrets, Actions workflows) runs exclusively as a Composio action via `COMPOSIO_EXECUTE`, using the organization's connected GitHub account.

## Finding the right action

There's no curated shortcut for repository management — everything here goes through discovery:

1. `COMPOSIO_SEARCH_TOOLS` with a plain-English `use_case`. Examples that map to real, common needs: "create a new GitHub repository", "fork a GitHub repository", "get repository details", "list workflows in a GitHub repository", "list recent GitHub Actions runs", "rerun a failed GitHub Actions workflow", "view branch protection rules for a repository", "update branch protection rules", "create a GitHub release", "list GitHub Actions secrets" (names only — values are never retrievable), "create or update a GitHub Actions secret".
2. `COMPOSIO_GET_TOOL_SCHEMAS` on the returned slug(s) — repository settings actions in particular have many optional fields (wiki/issues/projects toggles, merge-method allowances, default branch, topics), and you should only send the fields you actually intend to change.
3. `COMPOSIO_EXECUTE` with `{tool_slug, arguments}`, passing `connected_account_id` explicitly if the organization has more than one GitHub account connected.

## Workflow

### 1. Inspect

Before proposing any change, read the current state of whatever you're about to touch:
- Repository settings (visibility, default branch, wiki/issues toggles, merge-method allowances, topics).
- Branch protection rules on the target branch, if you're about to touch required checks, review counts, or admin enforcement.
- Existing releases, tags, and workflow list, if the task involves any of those.
- Secret names (never values — GitHub never returns a secret's plaintext, and neither should Docket ever imply it can).

### 2. Plan

State the exact mutation before applying it: which fields change, what stays the same, and — for anything with a blast radius beyond a single field (branch protection, visibility, collaborator permissions) — who else is affected. This is the point where you flag anything irreversible (a visibility flip that exposes history, a branch-protection loosening that removes a required check) so a human can weigh in before it happens, not after.

### 3. Apply

Call `COMPOSIO_EXECUTE` with only the fields the plan described, then re-fetch the resource (or use the response the action returns) to confirm the change actually took effect as described — GitHub's settings API in particular can silently ignore a field it doesn't recognize rather than erroring.

## Common operations and their shape

- **Create/fork a repository** — needs name, owner (user or org), visibility, and optionally a template or license. Forking returns immediately but the fork may take a moment to become clonable server-side; don't treat a slightly-delayed 404 on the new fork as a failure.
- **Repository settings** — PATCH-style action taking only the fields you want to change (description, visibility, has_wiki, has_issues, allow_auto_merge, default_branch); anything omitted is left as-is.
- **Branch protection** — a full-replace style action in GitHub's API: setting protection typically requires specifying the complete rule set (required status check contexts, required review count, admin enforcement) each time, not just the one field you want to change — read the existing rule set first and merge your change into the full object rather than sending a partial one that could silently drop other protections.
- **Releases** — tag name, release name, body/notes (or auto-generated notes), draft/prerelease flags. Uploading a binary asset to a release is a separate action from creating the release itself.
- **Actions workflows** — listing workflows and runs is read-only; re-running a run, re-running only failed jobs, and manually dispatching a workflow (`workflow_dispatch`) are writes with real CI/CD consequences.
- **Actions secrets** — creating/updating a secret requires the value to be encrypted against the repository's public key before it's sent (Composio's action handles this encryption internally — you supply the plaintext value as an argument, never a pre-encrypted blob). Listing secrets only ever returns names and update timestamps, never values.

## Gotchas specific to how Docket runs this

- **Approval gate.** Reads (repo info, branch protection, workflow/run lists, secret names) execute immediately. Any action whose slug reads as destructive — deleting a repository, branch, tag, or release — pauses for human approval automatically. Settings changes, branch-protection updates, releases, and secret writes are not automatically forced through approval by keyword alone, which means you carry the responsibility of stating the exact change and getting explicit sign-off in conversation before calling `COMPOSIO_EXECUTE` on anything with a blast radius beyond "purely additive."
- **Irreversible in practice, not just in name.** A repository or branch deletion, once approved and executed, is not something Docket can undo — GitHub's own restore window (where it exists) is short and not something this connector manages. Treat any deletion as final when describing it to the human.
- **Fork propagation delay.** A newly created fork or repository can 404 for a few seconds before GitHub finishes provisioning it — a follow-up read failing immediately after a successful create is not necessarily an error.
- **Multiple connected GitHub accounts** require explicit `connected_account_id` on `COMPOSIO_EXECUTE`, same as the other GitHub connector skills.
- **Never fabricate a topic, branch name, or secret name** you haven't confirmed exists — list first when precision matters (e.g., updating an existing secret vs. accidentally creating a new one with a typo'd name).
