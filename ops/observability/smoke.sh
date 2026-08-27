#!/usr/bin/env bash
set -euo pipefail

# Bounded local-stack verification. A real completed Toda span must already
# exist; health endpoints alone are not acceptance evidence.
: "${TODA_DB_PATH:?set TODA_DB_PATH to the running gateway SQLite database}"

curl --fail --silent --show-error --max-time 3 http://127.0.0.1:13133/
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:9090/-/ready
curl --fail --silent --show-error --max-time 3 http://127.0.0.1:3200/ready

trace_id="$(sqlite3 "$TODA_DB_PATH" "SELECT trace_id FROM otel_spans WHERE name = 'agent.invoke' ORDER BY start_time_ns DESC LIMIT 1;")"
if [[ -z "$trace_id" ]]; then
  echo "no completed agent.invoke span exists in SQLite" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 5 \
  "http://127.0.0.1:3200/api/traces/$trace_id" >/dev/null

metric_response="$(curl --fail --silent --show-error --max-time 5 \
  --get --data-urlencode 'query={__name__=~"toda_.+"}' \
  http://127.0.0.1:9090/api/v1/query)"
if ! grep -q '"status":"success"' <<<"$metric_response"; then
  echo "Prometheus did not return a successful Toda metric query" >&2
  exit 1
fi
if grep -q '"result":\[\]' <<<"$metric_response"; then
  echo "Prometheus returned no Toda metric samples" >&2
  exit 1
fi

echo "SQLite, Tempo, and Prometheus contain live Toda telemetry for trace $trace_id"
