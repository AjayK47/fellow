---
name: blogwatcher
display_name: Blog Watcher
description: Monitor selected blogs for meaningful new posts and summarize changes.
kind: builtin
platform_key: builtin.hermes.blogwatcher
activation: {mode: optional}
required_capabilities: {any_of: [web.search, web.fetch]}
warnings:
  - Never report a post as "new" without a stored baseline to compare against
    -- the first check of any blog is always a baseline, never a digest.
  - Stay silent (or say briefly that nothing changed) when nothing meaningful
    changed, especially on an automation-triggered run.
steps:
  - order: 1
    name: Check
    description: Fetch the blog's current posts and compare against the stored baseline.
  - order: 2
    name: Filter
    description: Keep only posts newer than the baseline and actually relevant to the watch.
  - order: 3
    name: Digest
    description: Update the baseline, then deliver a compact summary only if something changed.
---
There is no `blogwatcher-cli` binary or persistent daemon here -- this skill
is a pattern built from `web_fetch` (to read the blog or its feed) and the
shared team workspace (`workspace_read`/`workspace_write`) to remember what
was already seen. It's designed to run either ad hoc or, more usefully, as a
recurring `automation_create` goal.

## The baseline is a workspace file, not a database

Store the last-seen state as a small JSON or plain-text file under a
predictable workspace path, e.g. `watchers/blog-<slug>.json` containing the
most recent post title, URL, and publication date. Before generating any
digest:

1. `workspace_read` the baseline file for this blog. If it doesn't exist yet,
   this is the *first* check -- record the current posts as the baseline and
   say so; do not report them as new.
2. `web_fetch` the blog's homepage or its RSS/Atom feed URL if you know it
   (many blogs expose one at `/feed`, `/rss`, or `/atom.xml`; if unsure, fetch
   the homepage and look for a feed link, or ask the user for the feed URL
   once).
3. Compare the fetched posts against the baseline by URL (most reliable) or
   title + date if the URL isn't stable.
4. `workspace_write` the updated baseline with the newest post(s) before
   reporting anything -- so a delivery failure doesn't cause the same post to
   be re-reported next run.

## What counts as "new"

Only posts published after the baseline's most recent entry. Don't report a
post that merely changed formatting or got a minor edit -- compare on
identity (URL or a stable title), not on exact content match. If the fetch
returns posts you can't confidently date or identify, say that the check was
inconclusive rather than guessing.

## Delivering the digest

Keep it compact: title, one-line summary, and the source URL per new post --
this is a Slack message, not a document artifact, unless the user explicitly
wants a running archive (in which case use `brain_create_artifact` to persist
a durable digest log). If this check was triggered by a schedule
(`trigger_kind` is `automation`) and nothing changed, say so briefly or
produce no output at all rather than manufacturing an update -- the
automation firing is not itself news.

## Setting up the recurring watch

When a user asks to "watch this blog" or "notify me when X posts," call
`automation_create` with a sensible interval (hourly or daily is typical for
a blog) and a goal that names the blog and its URL explicitly, e.g. "Check
https://example.com/blog for new posts since the last check; deliver a short
digest only if there's something new." Remember this still pauses for human
approval before it takes effect -- say so rather than implying it's already
live.

## Multiple blogs

Track each blog under its own workspace path (`watchers/blog-<slug>.json`) so
one blog's baseline never overwrites another's, and only include the blogs
actually named or previously configured in this conversation -- don't invent
additional sources to watch.
