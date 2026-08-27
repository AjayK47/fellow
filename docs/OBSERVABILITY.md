# Local observability verification

The optional stack binds Collector, Tempo, and Prometheus ports to loopback
only. Toda's HTTP OTLP exporter must use the Collector trace endpoint ending
in `/v1/traces`; its metrics exporter derives the corresponding `/v1/metrics`
endpoint.

Start the stack with:

```bash
docker compose -f compose.observability.yaml up -d
```

After the gateway has completed at least one Task, run the bounded data-path
check with:

```bash
TODA_DB_PATH=/path/to/brain.db ops/observability/smoke.sh
```

It requires the same `agent.invoke` trace in SQLite and Tempo and a non-empty
Toda metric query in Prometheus. For the complete provider/connector readiness
and Collector-degradation check, run:

```bash
TODA_DB_PATH=/path/to/brain.db \
TODA_API_KEY=your-server-key \
ops/observability/live-acceptance.sh
```

Toda remains usable with SQLite telemetry when the optional stack is absent;
the local SQLite exporter is the source used by the dashboard and readiness
probe.

Images are pinned in `compose.observability.yaml`; update them deliberately,
then rerun the smoke check and verify a new Toda run in SQLite, Tempo, and the
Prometheus scrape endpoint.
