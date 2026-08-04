# Contributing to Docket

## Development setup

```bash
uv sync --extra dev
cp .env.example .env
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
uv run docket gateway
```

Seed a synthetic organization if you want data to look at immediately:

```bash
uv run docket-seed
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
