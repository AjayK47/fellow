# Contributing to Toda

## Development setup

```bash
uv sync --extra dev
cp config.example.json config.json && chmod 600 config.json
# set organization.owner_email; remove models.assignments to run offline
uv run toda init
```

No external key is required to run the full stack: with no `main` model
assigned, Toda uses deterministic model and embedding providers, which
exercise the complete ingestion, permission, memory, graph, policy, job, and
runtime machinery without paid inference.

Build the console once (skip this and the API still works, just without the
UI):

```bash
pnpm --dir web install
pnpm --dir web build
```

Start the gateway:

```bash
uv run toda gateway
```

Seed a synthetic organization if you want data to look at immediately:

```bash
uv run toda-seed
```

## Running the test suite

```bash
uv run pytest -q
```

For the console:

```bash
pnpm --dir web build   # typecheck + production build
```

Both are expected to pass cleanly before a change is considered done.

Run the public-release and provenance gate as well:

```bash
uv run python scripts/verify_release.py
uv run ruff check src tests scripts
```

## Working principles

- Prefer the simplest implementation that fully meets the stated
  requirement — no speculative abstractions for hypothetical future needs.
- Prefer established, well-maintained libraries over custom implementations.
- No backward-compatibility shims. If something is wrong, fix it in place
  rather than adding a flag to preserve the old behavior.
- Every write path (a tool call, a console action, an API request) goes
  through the same schema validation, policy check, and audit log as every
  other client — there is no privileged internal shortcut. Any change that
  needs to bypass that path is very likely the wrong change.
- Read `ARCHITECTURE.md` before making a structural change — most new work fits into an existing layer (gateway, harness,
  retrieval, reality, durability, console) rather than needing a new one.

## Before opening a pull request

- Run both test commands above.
- If you touched an API response shape, check the console pages that read
  it — several past bugs here were exactly this: a frontend field name that
  no longer matched what the backend actually returned.
- If you touched anything that reads `config.json` or handles a credential,
  double-check it still only writes to the local, gitignored file and never
  logs or echoes a secret value.
- If you touched a built-in skill, keep its reviewed `license` and `origin`
  frontmatter accurate. Inspiration, adaptation, and original implementation
  are different provenance claims.
- If your change affects a trust boundary, update `SECURITY.md` and include a
  focused regression test.

## Security and private data

Never use a public issue or pull request for vulnerabilities, credentials,
real company messages, connector payloads, databases, or workspaces. Follow
`SECURITY.md` for private vulnerability reporting and use synthetic fixtures
in tests and examples.
