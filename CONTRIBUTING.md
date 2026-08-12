# Contributing to Toda

## Development setup

```bash
uv sync --extra dev
python scripts/bootstrap.py
```

No external key is required to run the full stack — `.env.example` defaults
to deterministic model and embedding providers, which exercise the complete
ingestion, permission, memory, graph, policy, job, and runtime machinery
without paid inference.

Build the console once (skip this and the API still works, just without the
UI):

```bash
npm --prefix web install
npm --prefix web run build
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
cd web
npx tsc -b        # typecheck
npm run build     # production build
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
- Read `README.md` for the full architecture before making a structural
  change — most new work fits into an existing layer (gateway, harness,
  retrieval, reality, durability, console) rather than needing a new one.

## Before opening a pull request

- Run both test commands above.
- If you touched an API response shape, check the console pages that read
  it — several past bugs here were exactly this: a frontend field name that
  no longer matched what the backend actually returned.
- If you touched anything that writes to `.env` or handles a credential,
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
