# Company Brain

A Slack-native, company-wide agent harness with a Hermes-style model/tool loop,
permission-aware organizational memory, graph context, approval-gated actions,
and proactive attention.

SQLite is the definitive local database: relational state, graph nodes/edges,
FTS5 indexes, embedding vectors, run checkpoints, approvals, and the durable
job queue all live in one portable file.

## What is implemented

- Slack Events API and Socket Mode gateways, signature verification, mention/DM
  activation, ambient observation, thread sessions, and channel policies
- A bounded Hermes-style `model → tool → observation` loop with durable
  messages, turns, checkpoints, cancellation, budgets, and resumable waits
- Permission-filtered hybrid retrieval over raw evidence, structured memories,
  graph relationships, skills, and active runs
- OpenRouter chat, attention, extraction, and embedding clients plus fully
  deterministic offline providers
- Contextual proactive attention: retrieve first, small model second,
  deterministic safety policy last
- Composio Linear/GitHub tools with allowlists, JSON-schema validation,
  idempotency, tool policies, previews, Slack approvals, and continuation
- Asynchronous post-run extraction, inactivity-triggered Slack episode
  consolidation, graph evolution, profile refresh, and reindex jobs
- A leased SQLite job queue with retries, exponential backoff, dead letters,
  concurrent handlers, and a separately runnable worker process
- Automations that invoke the same permission and runtime path
- Full traces, audit events, feedback/evaluation metrics, retention controls,
  graph explorer, and an operational control room

## Setup

Use the requested `kys` conda environment and `uv`:

```bash
conda activate kys
uv sync --extra dev
cp .env.example .env
```

The zero-cost local mode needs no external provider:

```bash
uv run company-brain-seed
uv run company-brain
```

Open:

- Control room: <http://127.0.0.1:8080/admin>
- Knowledge graph: <http://127.0.0.1:8080/explorer>
- API documentation: <http://127.0.0.1:8080/docs>

The API process runs an in-process worker by default. For separate API and
worker processes:

```bash
COMPANY_BRAIN_WORKER_ENABLED=false uv run company-brain
uv run company-brain-worker
```

## Models and embeddings

For the embedding model already selected:

```dotenv
OPENROUTER_API_KEY=...
COMPANY_BRAIN_EMBEDDING_PROVIDER=openrouter
COMPANY_BRAIN_EMBEDDING_MODEL=qwen/qwen3-embedding-8b
```

To use live models for agent reasoning, proactive attention, and extraction:

```dotenv
COMPANY_BRAIN_MODEL_PROVIDER=openrouter
COMPANY_BRAIN_CHAT_MODEL=openai/gpt-4.1-mini
COMPANY_BRAIN_ATTENTION_MODEL=openai/gpt-4.1-mini
COMPANY_BRAIN_EXTRACTION_MODEL=openai/gpt-4.1-mini
```

Every new source is chunked and embedded once; every extracted atomic memory is
embedded once. Retrieval uses permission filtering before lexical, FTS5, and
exact cosine scoring. This is suitable for development and a small pilot.
Model inference is offloaded to OpenRouter, so local compute is light; the main
local costs are SQLite I/O and cosine scoring. At large vector counts, retain
the interfaces and replace exact scoring with `sqlite-vec` or a managed vector
index.

## Slack

For HTTP Events API delivery configure:

```dotenv
COMPANY_BRAIN_SLACK_SIGNING_SECRET=...
COMPANY_BRAIN_SLACK_BOT_TOKEN=xoxb-...
COMPANY_BRAIN_SLACK_BOT_USER_ID=U...
```

Point Slack event subscriptions at:

```text
POST https://your-host/v1/slack/events
POST https://your-host/v1/slack/interactions
```

For local Socket Mode, also set `COMPANY_BRAIN_SLACK_APP_TOKEN=xapp-...` and run:

```bash
uv run company-brain-slack
```

Register the workspace, Slack identities, and opted-in channel policies through
the API. Public ambient channels do not wake the agent unless their policy has
`proactive=true`. DMs and explicit mentions activate the main agent.

## Composio / Linear / GitHub

```dotenv
COMPANY_BRAIN_COMPOSIO_API_KEY=...
COMPANY_BRAIN_COMPOSIO_CONNECTED_ACCOUNT_ID=...
```

The V1 registry exposes only the architecture-approved Linear and GitHub tool
allowlist. Read tools can run directly. Write tools create a preview and
approval; trusted code—not the model—validates the approver before executing
the exact stored arguments. Refresh live Composio schemas with:

```text
POST /v1/tools:refresh
```

## Invoke and inspect the agent

```bash
curl -s http://127.0.0.1:8080/v1/agent/invoke \
  -H 'content-type: application/json' \
  -d '{
    "organization_id": "org_acme",
    "principal_id": "person_ajay",
    "conversation_id": "api:demo",
    "destination": {
      "type": "internal_agent_session",
      "id": "demo",
      "is_private": true
    },
    "goal": "Who owns the export worker and what happened recently?",
    "run_now": true
  }'
```

Use `GET /v1/agent/runs/{run_id}` for messages, model turns, tool calls,
approvals, and checkpoints.

Use `GET /v1/harness/status` to verify SQLite, model, embedding, Slack,
Composio, and worker readiness without returning any credential values.

## Realistic organization test

```bash
uv run python scripts/simulate_org.py \
  --db data/brain.db \
  --report reports/simulation.md
```

Add `--live-embeddings` to call the configured embedding model and
`--live-models` to call the configured chat models. The simulation sends Slack
Events API-shaped traffic and checks mention/DM activation, proactive wake and
suppression, deduplication, permission isolation, deliveries, memory capture,
and retrieval.

Run the automated suite:

```bash
uv run pytest -q
```

## Safety boundaries

V1 intentionally has no terminal, browser automation, arbitrary code
execution, subagents, dynamic tool loading, or automatic production writes.
Raw evidence is untrusted data, not instructions. Retrieval and delivery are
both reauthorized, and private memory is filtered by principal and destination.
