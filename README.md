# Toda

**A self-hosted organizational coworker for Slack.**

Toda reads the Slack conversations it is allowed to see, keeps
permission-scoped company memory, answers and acts when addressed, runs
substantial work as durable Tasks with specialist sub-agents, and asks before
doing anything that leaves Toda. It is one Python process on one SQLite file —
no external queue, cache or vector database.

> **Alpha.** Suitable for local evaluation and small self-hosted pilots. Not a
> multi-tenant service; do not expose it directly to the internet. Read
> [SECURITY.md](SECURITY.md).

```text
You:  What is blocking the Acme renewal?
Toda: searches authorized Slack, CRM, tickets and prior decisions
      → delegates CRM and engineering investigations to specialist Tasks
      → correlates the evidence into one cited answer
      → offers to update the account plan, and asks before writing to the CRM
```

How it works — the request path, Sessions and Tasks, tools and approval,
memory and retrieval — is in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Quick start

Requirements: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and Node.js with
`pnpm` only if you want the console UI.

```bash
uv sync --extra dev
cp config.example.json config.json
chmod 600 config.json            # required: config.json may hold credentials
# edit config.json: set organization.owner_email
uv run toda init                 # creates the org + owner account, prints the password once
pnpm --dir web install && pnpm --dir web build   # optional: builds the console
uv run toda gateway
```

Open <http://127.0.0.1:8080> and sign in as the owner. API docs are at
`/docs`, liveness at `/health`, runtime readiness at `/v1/harness/status`.

**Running without an API key:** remove the entries under
`models.assignments` and Toda uses its built-in deterministic model and
embedding providers. Nothing is billed and the whole runtime still runs. For
demo data, run `uv run toda-seed`.

## Configuration

`config.json` is the only configuration Toda reads. It is found at
`$TODA_CONFIG`, else the repo root, else the platform data directory. Unknown
keys are an error. `config.example.json` shows every section; the ones you
will touch first:

| Section | What to set |
|---|---|
| `organization` | `id`, `name`, `owner_email` |
| `models.providers` | Named connections (`openrouter`, `openai`, `anthropic`, `concentrate`, `xai`, `groq`, `deepinfra`, `custom`, `local`) with API keys |
| `models.assignments` | Which connection and model serve each role: `main`, `attention`, `extraction`, `embedding` (and optionally `tool_summary`, `vision`, `image`, `transcription`, `tts`) |
| `models.embedding_dimensions` | Must match the embedding model (384 for the local `BAAI/bge-small-en-v1.5`) |
| `slack` | `bot_token`, `signing_secret`, `bot_user_id`, and `app_token` for Socket Mode |
| `composio` | `api_key` to use connected apps (CRM, GitHub, Gmail, …) |
| `tools` | Brave / Firecrawl keys for web search and extraction; `run_code_enabled`, `browser_enabled` |
| `agent` | Iteration, tool-call, cost and duration budgets; child-Task limits; Pi coding workers |
| `worker` | Lane concurrency, attention confidence floor, proactive cooldown |

Restart the gateway after editing. Changing the embedding model changes the
vector width: startup re-embeds existing rows, and `toda reindex --force`
rebuilds the ANN index.

Local on-device embeddings need the extra: `uv sync --extra local-embed`.

## Connecting Slack

One gateway serves one Slack workspace.

- **Socket Mode** (local, no public URL): set `slack.app_token` (`xapp-…`) and
  `slack.bot_token`; `toda gateway` starts the listener.
- **HTTP Events API**: expose the server over HTTPS and point Slack at
  `POST /v1/slack/events` and `POST /v1/slack/interactions`; set
  `slack.signing_secret`.

DMs and mentions wake Toda directly. Other messages in observed channels go
through the attention classifier, which is biased toward staying quiet.
Channel observation and proactivity are managed from the console's Channels
page (`/v1/channel-policies`).

## Connecting apps

With `composio.api_key` set, connect accounts from the console's Connections
page. The agent reaches every connected toolkit through the generic
`integration_*` tools. For live triggers, set `server.public_url` (for example
an ngrok or cloudflared tunnel); the gateway registers
`/v1/composio/webhooks` with Composio at startup. MCP servers can be added
from the MCP page.

## Optional capabilities

These are off or inert until configured. Unavailable tools show as
`setup_needed` and are hidden from the model.

| Capability | Enable |
|---|---|
| Web search / page extraction | `tools.connections` Brave and Firecrawl keys |
| Code execution (`run_code`) | `tools.run_code_enabled` — **not sandboxed** |
| Browser automation | `uv sync --extra browser`, `tools.browser_enabled`, `tools.browser_allowed_domains` — **not sandboxed** |
| Pi coding workers | `agent.pi_coding_enabled` and `pi_*` settings — **not sandboxed** |
| Diagrams | Graphviz `dot` on the host |
| Infographics | `pnpm --dir src/toda/harness/infographic_runtime install` |
| Vision, image generation, speech | Assign the `vision`, `image`, `transcription`, `tts` roles |

## Using the API

Every `/v1` route needs a console login, except the Slack, Composio and event
ingestion endpoints.

```bash
curl -s -c jar http://127.0.0.1:8080/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email": "you@example.com", "password": "…"}'

curl -s -b jar http://127.0.0.1:8080/v1/tasks/invoke \
  -H 'content-type: application/json' \
  -d '{
    "organization_id": "org_acme",
    "conversation_id": "demo",
    "destination": {"type": "internal_agent_session", "id": "demo", "is_private": true},
    "prompt": "Who owns the export worker and what happened to it recently?",
    "run_now": true
  }'
```

External systems can push events to `POST /v1/events` from localhost, or from
a remote host with `Authorization: Bearer <one of server.api_keys>`. These are
stored as memory only; they do not wake Toda.

## Operations

| Task | Command |
|---|---|
| Run the API + Slack + workers | `uv run toda gateway` |
| Run the API + workers without Slack | `uv run toda serve` |
| Rebuild vector indexes | `uv run toda reindex [--force]` |
| Preview / apply retention | `uv run toda retention --organization-id org_acme --older-than-days 90 [--apply --confirmed-by <principal>]` |
| Delete an organization | `uv run toda retention --organization-id X --delete-organization --confirm X` |
| Wipe all local state | `uv run toda reset --yes` (database and workspaces, not `config.json`) |

Toda is a single process: the agent, knowledge and infrastructure worker
lanes run inside the gateway, and only one process may own the database.

Guides: [deployment](docs/DEPLOYMENT.md) ·
[backup and restore](docs/BACKUP_AND_RESTORE.md) ·
[observability](docs/OBSERVABILITY.md) · [evals](docs/EVALS.md) ·
[benchmarks](docs/BENCHMARKS.md) · [releasing](docs/RELEASING.md)

## Development

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/verify_release.py
pnpm --dir web build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
