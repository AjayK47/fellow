#!/usr/bin/env bash
set -euo pipefail

: "${TODA_BASE_URL:=http://127.0.0.1:8080}"
: "${TODA_DB_PATH:?set TODA_DB_PATH to the running gateway SQLite database}"
: "${TODA_API_KEY:?set TODA_API_KEY to a configured server API key}"

compose=(docker compose -f compose.observability.yaml)
auth=(-H "Authorization: Bearer $TODA_API_KEY")

"${compose[@]}" up -d --wait
ops/observability/smoke.sh

ready_before="$(curl --fail --silent --show-error --max-time 15 "${auth[@]}" \
  "$TODA_BASE_URL/v1/harness/readiness")"
if ! grep -q '"mode":"real_world_ready"' <<<"$ready_before"; then
  echo "provider, connector, or runtime readiness did not pass before degradation test" >&2
  exit 1
fi

"${compose[@]}" stop otel-collector
trap '"${compose[@]}" start otel-collector >/dev/null 2>&1 || true' EXIT
sleep 3
degraded="$(curl --fail --silent --show-error --max-time 15 "${auth[@]}" \
  "$TODA_BASE_URL/v1/harness/readiness")"
if ! grep -q 'Collector health probe failed' <<<"$degraded"; then
  echo "readiness did not degrade when the Collector stopped" >&2
  "${compose[@]}" start otel-collector
  exit 1
fi

"${compose[@]}" start otel-collector
trap - EXIT
echo "live observability and readiness-degradation acceptance passed"
