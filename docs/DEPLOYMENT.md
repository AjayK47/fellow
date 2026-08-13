# Self-hosted deployment

## Local Compose installation

```bash
python3 scripts/bootstrap.py
docker compose up --build -d
docker compose exec toda toda-seed
```

Open <http://127.0.0.1:8080> and sign in with the administrator token printed
by the bootstrap command. The deterministic provider requires no paid API key.

The Compose port is deliberately bound to loopback. Data is stored in the
`toda-data` named volume.

## Connecting a model and Slack

Edit `config.json` and restart the container after setting the desired provider and
Slack credentials. Use Socket Mode for a local installation; it does not
require a public webhook endpoint. See `config.example.json` and the console
page for the complete configuration surface.

## Network deployment

Do not change the port mapping to `0.0.0.0` without an authenticated TLS
reverse proxy. Keep dashboard authentication enabled, use a secret manager,
set remote ingestion keys, and follow `SECURITY.md`.

For larger deployments, run `toda serve` and `toda-worker` as separate
processes against the same protected database volume. SQLite remains the
supported community backend; only one host should write the database file.

## Upgrades

1. Back up the database and workspace volume.
2. Read `CHANGELOG.md` for breaking changes.
3. Pull the new image or source.
4. Rebuild and restart.
5. Confirm `/health`, worker activity, Slack delivery, and a restore test.
