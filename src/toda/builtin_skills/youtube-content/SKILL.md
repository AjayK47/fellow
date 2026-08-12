---
name: youtube-content
display_name: YouTube Content
description: Research and summarize authorized or public YouTube content with timestamps.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
  inspired_by: hermes-agent
kind: builtin
platform_key: builtin.hermes.youtube_content
activation: {mode: optional}
required_capabilities: {any_of: [web.search, web.fetch]}
warnings:
  - There is no transcript-extraction library or shell available here --
    only web_search and web_fetch. Never invent timestamps, quotes, or
    transcript text that wasn't actually in a fetched page.
  - If no transcript or description text is retrievable, say so plainly
    instead of summarizing from the video's title alone.
steps:
  - order: 1
    name: Locate
    description: Resolve the exact video, channel, and publication date.
  - order: 2
    name: Extract
    description: Fetch whatever real text is available -- description, captions endpoint, or a companion source.
  - order: 3
    name: Summarize
    description: Produce the requested format, citing only evidence actually retrieved.
---
Toda cannot run `youtube-transcript-api` or any other local dependency --
this skill is built entirely from `web_search` and `web_fetch`. Be upfront
about that ceiling: some videos will yield a rich summary, others will yield
only a title, description, and upload date, and a few will yield nothing
retrievable at all.

## Resolve the exact video first

If the user gives a URL, normalize it to the video ID (the `v=` parameter,
or the path segment for `youtu.be/<id>` and `/shorts/<id>` links) and fetch
`https://www.youtube.com/watch?v=<id>` with `web_fetch`. If the user
describes a video without a link ("that talk on X by channel Y"), use
`web_search` to find the specific video first -- confirm the channel and
approximate publish date before treating it as the right one; don't guess
from a vague description.

## What's actually extractable

- **Description and metadata**: `web_fetch` on the watch page URL typically
  surfaces the title, channel name, upload date, and the video's own
  description text -- often the most reliable source, since many creators
  put a chapter list or summary directly in the description.
- **Captions**: some videos expose a public caption track at a predictable
  URL pattern (`https://www.youtube.com/api/timedtext?lang=en&v=<id>`); try
  fetching it, but treat availability as unreliable -- many videos don't
  serve it publicly, and the response may be empty or require a different
  language code. If it returns real caption text, it usually includes
  timing information you can convert into timestamps.
- **Comments/community summaries**: occasionally a pinned comment or the
  top comments contain a chapter list -- only use this if `web_fetch`
  actually surfaced it, and attribute it as a comment, not the creator's
  own words.

If none of the above yields usable text, tell the user plainly that the
transcript isn't retrievable here and ask them to paste the transcript or
description if they have it -- don't produce a summary from the title and
thumbnail alone and present it as if it reflects the video's content.

## Producing the requested format

Once real text is in hand, shape it to what was asked:

- **Summary**: a concise overview grounded only in the retrieved text.
- **Chapters**: a timestamped list, but only include timestamps that came
  from an actual caption/description source -- never estimate or invent
  timing.
- **Blog post / thread**: restructure the content into the target format
  while preserving factual claims; run the humanizer skill afterward if the
  output reads stiff or AI-generated.
- **Quotes**: pull exact phrasing from the retrieved text, not a
  paraphrase, and note it's a direct quote from the description/captions.

If the user didn't specify a format, default to a short summary rather than
guessing which format they want.

## Distinguish the speaker's claims from your own inference

When summarizing opinionated or claim-heavy content (a review, a technical
talk, a debate), keep the speaker's stated claims separate from any
inference or evaluation you add. Attribute claims to the video explicitly
(for example, "the video argues that X") rather than presenting them as
established fact.

## Citing the source

Always include the exact video URL and the channel/upload date you
confirmed, so the user (or a later reader) can verify against the original.
