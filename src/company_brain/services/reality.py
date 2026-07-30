"""Company reality engine: canonical objects, identity, temporal facts, cases, and judgment."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from company_brain.domain.models import new_id, utcnow


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _load(value: str) -> dict[str, Any]:
    return json.loads(value)


def _now() -> str:
    return utcnow().isoformat()


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


class RealityEngine:
    """Evidence-first materialization of the current and historical company state."""

    SINGLE_VALUED_PREDICATES = {
        "owner",
        "owned_by",
        "account_owner",
        "assignee",
        "status",
        "state",
        "priority",
        "launch_date",
        "release_date",
        "primary_store",
        "uses_primary_store",
        "on_call",
        "on_call_for",
    }
    PREDICATE_ALIASES = {
        "is_owned_by": "owner",
        "owned_by": "owner",
        "owner_is": "owner",
        "account_owned_by": "account_owner",
        "assigned_to": "assignee",
        "is_assigned_to": "assignee",
        "current_status": "status",
        "has_status": "status",
        "primary_database": "primary_store",
        "database": "primary_store",
        "uses_database": "primary_store",
        "release_date": "launch_date",
        "ship_date": "launch_date",
    }
    CASE_MEMORY_TYPES = {"issue", "commitment", "decision", "outcome"}

    def __init__(self, store: Any, graph: Any, audit: Any) -> None:
        self.store = store
        self.graph = graph
        self.audit = audit

    # ------------------------------------------------------------------
    # Canonical connector objects and resumable syncs
    # ------------------------------------------------------------------

    def ingest_source(
        self, source: dict[str, Any], memories: list[dict[str, Any]]
    ) -> dict[str, Any]:
        obj = self.upsert_connector_object_from_source(source)
        entity_ids = self.resolve_source_entities(source, obj)
        if entity_ids:
            source["entity_ids"] = sorted(
                set(source.get("entity_ids") or []) | set(entity_ids)
            )
            self.store.upsert_source(source)
            for memory in memories:
                memory["entity_ids"] = sorted(
                    set(memory.get("entity_ids") or []) | set(entity_ids)
                )
                self.store.upsert_memory(memory)
        facts = [
            fact
            for memory in memories
            if (fact := self.materialize_memory(memory, source=source))
        ]
        case = self.correlate_case(source, memories, entity_ids)
        return {
            "connector_object": obj,
            "entity_ids": entity_ids,
            "facts": facts,
            "case": case,
        }

    def upsert_connector_object_from_source(
        self, source: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = source.get("metadata") or {}
        raw = metadata.get("raw_data")
        data = raw if isinstance(raw, dict) else {
            "content": source.get("content") or "",
            "metadata": metadata,
        }
        provider = str(source.get("source_system") or "unknown").lower()
        object_type = str(
            metadata.get("object_type")
            or source.get("source_type")
            or "event"
        ).lower()
        external_id = str(
            metadata.get("object_id")
            or source.get("external_id")
            or source["id"]
        )
        occurred_at = source.get("occurred_at")
        account_external = metadata.get("connected_account_id")
        account = (
            self.store.connector_account_by_external_id(str(account_external))
            if account_external
            else None
        )
        return self.upsert_connector_object(
            organization_id=source["organization_id"],
            connector_account_id=(account or {}).get("id"),
            provider=provider,
            object_type=object_type,
            external_id=external_id,
            data=data,
            occurred_at=occurred_at,
            source_id=source["id"],
            deleted=bool(source.get("deleted_at")),
        )

    def upsert_connector_object(
        self,
        *,
        organization_id: str,
        connector_account_id: str | None,
        provider: str,
        object_type: str,
        external_id: str,
        data: dict[str, Any],
        occurred_at: str | None = None,
        source_id: str | None = None,
        deleted: bool = False,
    ) -> dict[str, Any]:
        payload_hash = "sha256:" + hashlib.sha256(
            _dump(data).encode()
        ).hexdigest()
        with self.store.cursor() as cur:
            cur.execute(
                """SELECT payload FROM connector_objects
                WHERE organization_id = ? AND provider = ? AND object_type = ?
                  AND external_id = ?
                ORDER BY version DESC LIMIT 1""",
                (organization_id, provider, object_type, external_id),
            )
            row = cur.fetchone()
            previous = _load(row["payload"]) if row else None
            if (
                previous
                and previous["payload_hash"] == payload_hash
                and bool(previous.get("deleted_at")) == deleted
            ):
                return previous
            version = int((previous or {}).get("version", 0)) + 1
            now = _now()
            record = {
                "id": new_id("object"),
                "organization_id": organization_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "object_type": object_type,
                "external_id": external_id,
                "version": version,
                "payload_hash": payload_hash,
                "occurred_at": occurred_at,
                "observed_at": now,
                "deleted_at": now if deleted else None,
                "source_id": source_id,
                "data": data,
            }
            cur.execute(
                """INSERT INTO connector_objects
                (id, organization_id, connector_account_id, provider, object_type,
                 external_id, version, payload_hash, occurred_at, observed_at,
                 deleted_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    organization_id,
                    connector_account_id,
                    provider,
                    object_type,
                    external_id,
                    version,
                    payload_hash,
                    occurred_at,
                    now,
                    record["deleted_at"],
                    _dump(record),
                ),
            )
        return record

    def begin_sync(
        self,
        *,
        organization_id: str,
        connector_account_id: str,
        object_type: str,
        mode: str = "incremental",
    ) -> dict[str, Any]:
        cursor = self.get_sync_cursor(
            organization_id, connector_account_id, object_type
        )
        now = _now()
        run = {
            "id": new_id("sync"),
            "organization_id": organization_id,
            "connector_account_id": connector_account_id,
            "object_type": object_type,
            "mode": mode,
            "status": "running",
            "cursor_before": (cursor or {}).get("cursor"),
            "cursor_after": None,
            "processed": 0,
            "created_at": now,
            "updated_at": now,
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO connector_sync_runs
                (id, organization_id, connector_account_id, object_type, status,
                 cursor_before, cursor_after, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run["id"],
                    organization_id,
                    connector_account_id,
                    object_type,
                    "running",
                    run["cursor_before"],
                    None,
                    _dump(run),
                    now,
                    now,
                ),
            )
        return run

    def apply_sync_batch(
        self,
        sync_run_id: str,
        *,
        provider: str,
        records: list[dict[str, Any]],
        next_cursor: str | None,
        scope_id: str = "scope_org_public",
    ) -> dict[str, Any]:
        run = self.get_sync_run(sync_run_id)
        if not run or run["status"] != "running":
            raise ValueError("sync run is not active")
        objects = []
        for item in records:
            external_id = item.get("id") or item.get("external_id")
            if external_id is None:
                raise ValueError("every backfill record requires id or external_id")
            obj = self.upsert_connector_object(
                organization_id=run["organization_id"],
                connector_account_id=run["connector_account_id"],
                provider=provider,
                object_type=run["object_type"],
                external_id=str(external_id),
                data=item,
                occurred_at=item.get("occurred_at")
                or item.get("updated_at")
                or item.get("created_at"),
                deleted=bool(item.get("deleted") or item.get("deleted_at")),
            )
            objects.append(obj)
        run["processed"] = int(run.get("processed", 0)) + len(records)
        run["cursor_after"] = next_cursor
        run["updated_at"] = _now()
        self._save_sync_run(run)
        self._save_sync_cursor(
            run["organization_id"],
            run["connector_account_id"],
            run["object_type"],
            next_cursor,
            scope_id=scope_id,
        )
        return {"sync_run": run, "objects": objects}

    def complete_sync(self, sync_run_id: str, *, error: str | None = None) -> dict[str, Any]:
        run = self.get_sync_run(sync_run_id)
        if not run:
            raise KeyError(sync_run_id)
        run["status"] = "failed" if error else "completed"
        run["error"] = error
        run["updated_at"] = _now()
        self._save_sync_run(run)
        self.audit.record(
            run["organization_id"],
            "connector.sync.completed" if not error else "connector.sync.failed",
            {"sync_run_id": run["id"], "processed": run.get("processed", 0), "error": error},
        )
        return run

    def get_sync_run(self, run_id: str) -> dict[str, Any] | None:
        with self.store.cursor() as cur:
            cur.execute(
                "SELECT payload FROM connector_sync_runs WHERE id = ?", (run_id,)
            )
            row = cur.fetchone()
        return _load(row["payload"]) if row else None

    def list_sync_runs(self, organization_id: str) -> list[dict[str, Any]]:
        return self._list_payloads(
            "connector_sync_runs",
            "organization_id = ?",
            (organization_id,),
            "created_at DESC",
        )

    def list_connector_objects(
        self,
        organization_id: str,
        *,
        provider: str | None = None,
        object_type: str | None = None,
        external_id: str | None = None,
        latest_only: bool = True,
        scope_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if object_type:
            clauses.append("object_type = ?")
            params.append(object_type)
        if external_id:
            clauses.append("external_id = ?")
            params.append(external_id)
        with self.store.cursor() as cur:
            cur.execute(
                f"""SELECT payload FROM connector_objects
                WHERE {' AND '.join(clauses)}
                ORDER BY provider, object_type, external_id, version DESC""",
                params,
            )
            values = [_load(row["payload"]) for row in cur.fetchall()]
        if not latest_only:
            result = values
        else:
            latest: dict[tuple[str, str, str], dict[str, Any]] = {}
            for value in values:
                key = (
                    value["provider"],
                    value["object_type"],
                    value["external_id"],
                )
                latest.setdefault(key, value)
            result = list(latest.values())
        if scope_ids is None:
            return result
        allowed = set(scope_ids)
        visible = []
        for value in result:
            source = (
                self.store.get_source(value["source_id"])
                if value.get("source_id")
                else None
            )
            account = (
                self.store.get_harness_record(
                    "connector_accounts", value["connector_account_id"]
                )
                if value.get("connector_account_id")
                else None
            )
            scope_id = (source or {}).get("scope_id") or (account or {}).get(
                "scope_id"
            )
            if scope_id in allowed:
                visible.append(value)
        return visible

    def get_sync_cursor(
        self, organization_id: str, connector_account_id: str, object_type: str
    ) -> dict[str, Any] | None:
        with self.store.cursor() as cur:
            cur.execute(
                """SELECT payload FROM connector_sync_cursors
                WHERE organization_id = ? AND connector_account_id = ?
                  AND object_type = ?""",
                (organization_id, connector_account_id, object_type),
            )
            row = cur.fetchone()
        return _load(row["payload"]) if row else None

    def _save_sync_run(self, run: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """UPDATE connector_sync_runs
                SET status = ?, cursor_after = ?, payload = ?, updated_at = ?
                WHERE id = ?""",
                (
                    run["status"],
                    run.get("cursor_after"),
                    _dump(run),
                    run["updated_at"],
                    run["id"],
                ),
            )

    def _save_sync_cursor(
        self,
        organization_id: str,
        connector_account_id: str,
        object_type: str,
        cursor: str | None,
        *,
        scope_id: str,
    ) -> None:
        now = _now()
        record = {
            "organization_id": organization_id,
            "connector_account_id": connector_account_id,
            "object_type": object_type,
            "cursor": cursor,
            "scope_id": scope_id,
            "updated_at": now,
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO connector_sync_cursors
                (organization_id, connector_account_id, object_type, cursor,
                 payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, connector_account_id, object_type)
                DO UPDATE SET cursor=excluded.cursor, payload=excluded.payload,
                              updated_at=excluded.updated_at""",
                (
                    organization_id,
                    connector_account_id,
                    object_type,
                    cursor,
                    _dump(record),
                    now,
                ),
            )

    # ------------------------------------------------------------------
    # Entity resolution
    # ------------------------------------------------------------------

    def resolve_source_entities(
        self, source: dict[str, Any], obj: dict[str, Any]
    ) -> list[str]:
        identifiers = self._extract_identifiers(
            obj["provider"], obj.get("data") or {}
        )
        resolved: set[str] = set(source.get("entity_ids") or [])
        author = source.get("author_entity_id")
        if author:
            resolved.add(author)
        for identifier in identifiers:
            existing = self.lookup_identifier(
                source["organization_id"],
                identifier["namespace"],
                identifier["value"],
            )
            if existing:
                resolved.add(existing["entity_id"])
                continue
            entity = self._candidate_entity(
                source["organization_id"], identifier
            )
            if entity and identifier["strength"] == "hard":
                self.link_identifier(
                    organization_id=source["organization_id"],
                    entity_id=entity["id"],
                    namespace=identifier["namespace"],
                    value=identifier["value"],
                    confidence=1.0,
                    verified=True,
                    source_id=source["id"],
                )
                resolved.add(entity["id"])
            elif identifier["strength"] == "hard":
                entity_id = new_id("entity")
                namespace = identifier["namespace"]
                entity_type = (
                    "customer"
                    if any(
                        marker in namespace
                        for marker in ("customer_id", "account_id", "contact_id")
                    )
                    else "service"
                    if "repository_id" in namespace
                    else "person"
                )
                self.graph.upsert_entity(
                    {
                        "id": entity_id,
                        "organization_id": source["organization_id"],
                        "entity_type": entity_type,
                        "name": identifier.get("label")
                        or identifier["value"],
                        "aliases": [identifier["value"]],
                        "attributes": {
                            "created_by_entity_resolution": True,
                            "source_id": source["id"],
                        },
                    }
                )
                self.link_identifier(
                    organization_id=source["organization_id"],
                    entity_id=entity_id,
                    namespace=identifier["namespace"],
                    value=identifier["value"],
                    confidence=0.95,
                    verified=False,
                    source_id=source["id"],
                )
                resolved.add(entity_id)
            elif entity:
                self._create_resolution_candidate(
                    source["organization_id"],
                    entity,
                    identifier,
                    source["id"],
                )
        return sorted(resolved)

    def link_identifier(
        self,
        *,
        organization_id: str,
        entity_id: str,
        namespace: str,
        value: str,
        confidence: float,
        verified: bool,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize(value)
        existing = self.lookup_identifier(
            organization_id, namespace, normalized
        )
        if existing and existing["entity_id"] != entity_id:
            raise ValueError("identifier is already linked to another entity")
        record = existing or {
            "id": new_id("identifier"),
            "organization_id": organization_id,
            "entity_id": entity_id,
            "namespace": namespace,
            "normalized_value": normalized,
            "confidence": float(confidence),
            "verified": bool(verified),
            "source_ids": [],
            "created_at": _now(),
        }
        if source_id and source_id not in record["source_ids"]:
            record["source_ids"].append(source_id)
        record["confidence"] = max(float(record["confidence"]), float(confidence))
        record["verified"] = bool(record["verified"] or verified)
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO external_identifiers
                (id, organization_id, entity_id, namespace, normalized_value,
                 confidence, verified, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, namespace, normalized_value)
                DO UPDATE SET entity_id=excluded.entity_id,
                              confidence=excluded.confidence,
                              verified=excluded.verified,
                              payload=excluded.payload""",
                (
                    record["id"],
                    organization_id,
                    entity_id,
                    namespace,
                    normalized,
                    record["confidence"],
                    int(record["verified"]),
                    _dump(record),
                    record["created_at"],
                ),
            )
        return record

    def lookup_identifier(
        self, organization_id: str, namespace: str, value: str
    ) -> dict[str, Any] | None:
        with self.store.cursor() as cur:
            cur.execute(
                """SELECT payload FROM external_identifiers
                WHERE organization_id = ? AND namespace = ?
                  AND normalized_value = ?""",
                (organization_id, namespace, _normalize(value)),
            )
            row = cur.fetchone()
        return _load(row["payload"]) if row else None

    def list_identifiers(
        self, organization_id: str, entity_id: str | None = None
    ) -> list[dict[str, Any]]:
        if entity_id:
            return self._list_payloads(
                "external_identifiers",
                "organization_id = ? AND entity_id = ?",
                (organization_id, entity_id),
                "created_at",
            )
        return self._list_payloads(
            "external_identifiers",
            "organization_id = ?",
            (organization_id,),
            "created_at",
        )

    def list_resolution_candidates(
        self, organization_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        where = "organization_id = ?"
        params: tuple[Any, ...] = (organization_id,)
        if status:
            where += " AND status = ?"
            params += (status,)
        return self._list_payloads(
            "entity_resolution_candidates", where, params, "created_at DESC"
        )

    def decide_resolution_candidate(
        self, candidate_id: str, *, approved: bool, decided_by: str
    ) -> dict[str, Any]:
        with self.store.cursor() as cur:
            cur.execute(
                "SELECT payload FROM entity_resolution_candidates WHERE id = ?",
                (candidate_id,),
            )
            row = cur.fetchone()
        if not row:
            raise KeyError(candidate_id)
        candidate = _load(row["payload"])
        if candidate["status"] != "pending":
            raise ValueError("candidate has already been decided")
        candidate["status"] = "approved" if approved else "rejected"
        candidate["decided_by"] = decided_by
        candidate["updated_at"] = _now()
        if approved:
            self.link_identifier(
                organization_id=candidate["organization_id"],
                entity_id=candidate["left_entity_id"],
                namespace=candidate["identifier"]["namespace"],
                value=candidate["identifier"]["value"],
                confidence=candidate["score"],
                verified=True,
                source_id=candidate.get("source_id"),
            )
        with self.store.cursor() as cur:
            cur.execute(
                """UPDATE entity_resolution_candidates
                SET status = ?, payload = ?, updated_at = ? WHERE id = ?""",
                (
                    candidate["status"],
                    _dump(candidate),
                    candidate["updated_at"],
                    candidate_id,
                ),
            )
        return candidate

    def _extract_identifiers(
        self, provider: str, data: dict[str, Any]
    ) -> list[dict[str, str]]:
        found: dict[tuple[str, str], dict[str, str]] = {}

        def visit(value: Any, path: str = "") -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    lower = str(key).lower()
                    if isinstance(child, (str, int)):
                        text = str(child).strip()
                        if "email" in lower and "@" in text:
                            item = {
                                "namespace": "email",
                                "value": text,
                                "strength": "hard",
                                "label": text.split("@", 1)[0],
                            }
                            found[(item["namespace"], _normalize(text))] = item
                        elif lower in {
                            "user_id",
                            "customer_id",
                            "account_id",
                            "contact_id",
                            "assignee_id",
                            "owner_id",
                            "repository_id",
                        }:
                            namespace = f"{provider}:{lower}"
                            stem = lower[:-3] if lower.endswith("_id") else lower
                            item = {
                                "namespace": namespace,
                                "value": text,
                                "strength": "hard",
                                "label": str(
                                    value.get(f"{stem}_name")
                                    or value.get("name")
                                    or value.get("display_name")
                                    or text
                                ),
                            }
                            found[(namespace, _normalize(text))] = item
                        elif lower in {"name", "display_name", "full_name"}:
                            namespace = f"{provider}:name"
                            item = {
                                "namespace": namespace,
                                "value": text,
                                "strength": "soft",
                                "label": text,
                            }
                            found[(namespace, _normalize(text))] = item
                    visit(child, child_path)
            elif isinstance(value, list):
                for child in value[:100]:
                    visit(child, path)

        visit(data)
        return list(found.values())

    def _candidate_entity(
        self, organization_id: str, identifier: dict[str, str]
    ) -> dict[str, Any] | None:
        label = _normalize(identifier.get("label"))
        value = _normalize(identifier["value"])
        if identifier["namespace"] == "email":
            local = value.split("@", 1)[0].replace(".", " ").replace("_", " ")
            label = local
        best: tuple[float, dict[str, Any]] | None = None
        for entity in self.store.list_entities(organization_id):
            candidates = [entity.get("name") or "", *(entity.get("aliases") or [])]
            score = max(
                (
                    SequenceMatcher(None, label, _normalize(name)).ratio()
                    for name in candidates
                    if name
                ),
                default=0.0,
            )
            if score >= 0.72 and (best is None or score > best[0]):
                best = (score, entity)
        return best[1] if best else None

    def _create_resolution_candidate(
        self,
        organization_id: str,
        entity: dict[str, Any],
        identifier: dict[str, str],
        source_id: str,
    ) -> dict[str, Any]:
        score = SequenceMatcher(
            None,
            _normalize(entity.get("name")),
            _normalize(identifier.get("label")),
        ).ratio()
        now = _now()
        candidate = {
            "id": new_id("resolution"),
            "organization_id": organization_id,
            "left_entity_id": entity["id"],
            "right_entity_id": f"unresolved:{identifier['namespace']}:{_normalize(identifier['value'])}",
            "identifier": identifier,
            "signals": [{"kind": "name_similarity", "score": score}],
            "score": score,
            "status": "pending",
            "source_id": source_id,
            "created_at": now,
            "updated_at": now,
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO entity_resolution_candidates
                (id, organization_id, left_entity_id, right_entity_id, status,
                 score, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate["id"],
                    organization_id,
                    candidate["left_entity_id"],
                    candidate["right_entity_id"],
                    "pending",
                    score,
                    _dump(candidate),
                    now,
                    now,
                ),
            )
        return candidate

    # ------------------------------------------------------------------
    # Bi-temporal facts and contradictions
    # ------------------------------------------------------------------

    def materialize_memory(
        self, memory: dict[str, Any], *, source: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        claim = memory.get("canonical_claim") or {}
        if not all(claim.get(key) for key in ("subject", "predicate", "object")):
            return None
        subject_id = self._resolve_claim_subject(memory, claim["subject"])
        return self.assert_fact(
            organization_id=memory["organization_id"],
            subject_id=subject_id,
            predicate=claim["predicate"],
            object_value=claim["object"],
            scope_id=memory["scope_id"],
            valid_from=memory.get("valid_from")
            or (source or {}).get("occurred_at")
            or _now(),
            valid_to=memory.get("valid_until"),
            source_ids=memory.get("source_ids") or [],
            memory_id=memory["id"],
            confidence=float(memory.get("confidence") or 0.5),
            authority=float(memory.get("authority") or 0.5),
        )

    def assert_fact(
        self,
        *,
        organization_id: str,
        subject_id: str,
        predicate: str,
        object_value: str,
        scope_id: str,
        valid_from: str,
        valid_to: str | None = None,
        source_ids: list[str] | None = None,
        memory_id: str | None = None,
        confidence: float = 0.8,
        authority: float = 0.8,
    ) -> dict[str, Any]:
        predicate_key = self._predicate_key(predicate)
        object_key = _normalize(object_value)
        current = self.current_facts(
            organization_id, subject_id=subject_id, predicate=predicate_key
        )
        for fact in current:
            if _normalize(fact["object_value"]) == object_key:
                merged = sorted(
                    set(fact.get("source_ids") or []) | set(source_ids or [])
                )
                fact["source_ids"] = merged
                fact["confidence"] = max(float(fact["confidence"]), confidence)
                fact["authority"] = max(float(fact["authority"]), authority)
                self._save_fact(fact)
                return fact

        now = _now()
        fact = {
            "id": new_id("fact"),
            "organization_id": organization_id,
            "subject_id": subject_id,
            "predicate": predicate_key,
            "object_value": object_value,
            "status": "active",
            "valid_from": valid_from,
            "valid_to": valid_to,
            "system_from": now,
            "system_to": None,
            "scope_id": scope_id,
            "source_ids": source_ids or [],
            "memory_id": memory_id,
            "confidence": confidence,
            "authority": authority,
        }
        competing = [
            existing
            for existing in current
            if self._intervals_overlap(
                existing.get("valid_from"),
                existing.get("valid_to"),
                valid_from,
                valid_to,
            )
        ]
        single = (
            predicate_key in self.SINGLE_VALUED_PREDICATES
            or predicate_key.endswith(("_owner", "_status", "_assignee"))
        )
        if competing:
            conflict = self._record_conflict(fact, competing, single_valued=single)
            fact["conflict_id"] = conflict["id"]
            if single:
                strongest = max(
                    competing,
                    key=lambda item: (
                        float(item.get("authority") or 0),
                        float(item.get("confidence") or 0),
                    ),
                )
                if (authority, confidence) >= (
                    float(strongest.get("authority") or 0),
                    float(strongest.get("confidence") or 0),
                ):
                    for old in competing:
                        old["status"] = "superseded"
                        old["system_to"] = now
                        old["valid_to"] = valid_from
                        old["superseded_by"] = fact["id"]
                        self._save_fact(old)
                    conflict["status"] = "resolved"
                    conflict["resolution"] = {
                        "winner_fact_id": fact["id"],
                        "method": "authority_then_confidence",
                    }
                    self._save_conflict(conflict)
                else:
                    fact["status"] = "contested"
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO reality_facts
                (id, organization_id, subject_id, predicate, object_value, status,
                 valid_from, valid_to, system_from, system_to, scope_id, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fact["id"],
                    organization_id,
                    subject_id,
                    predicate_key,
                    object_value,
                    fact["status"],
                    valid_from,
                    valid_to,
                    now,
                    None,
                    scope_id,
                    _dump(fact),
                ),
            )
        return fact

    def current_facts(
        self,
        organization_id: str,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        valid_at: str | None = None,
        known_at: str | None = None,
        scope_ids: list[str] | None = None,
        include_contested: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["organization_id = ?"]
        params: list[Any] = [organization_id]
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if predicate:
            clauses.append("predicate = ?")
            params.append(self._predicate_key(predicate))
        if scope_ids is not None:
            if not scope_ids:
                return []
            clauses.append(f"scope_id IN ({','.join('?' for _ in scope_ids)})")
            params.extend(scope_ids)
        facts = self._list_payloads(
            "reality_facts", " AND ".join(clauses), tuple(params), "system_from"
        )
        at = valid_at or _now()
        known = known_at or _now()
        values = [
            fact
            for fact in facts
            if fact.get("status") not in {"retracted", "deleted"}
            and (include_contested or fact.get("status") != "contested")
            and (not fact.get("valid_from") or fact["valid_from"] <= at)
            and (not fact.get("valid_to") or fact["valid_to"] > at)
            and fact["system_from"] <= known
            and (not fact.get("system_to") or fact["system_to"] > known)
        ]
        return [fact for fact in values if self._fact_has_live_source(fact)]

    def list_conflicts(
        self,
        organization_id: str,
        status: str | None = None,
        scope_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        where = "organization_id = ?"
        params: tuple[Any, ...] = (organization_id,)
        if status:
            where += " AND status = ?"
            params += (status,)
        conflicts = self._list_payloads(
            "fact_conflicts", where, params, "created_at DESC"
        )
        if scope_ids is None:
            return conflicts
        allowed = set(scope_ids)
        visible = []
        for conflict in conflicts:
            facts = [
                self._get_payload("reality_facts", fact_id)
                for fact_id in conflict.get("fact_ids") or []
            ]
            facts = [fact for fact in facts if fact]
            if facts and all(fact.get("scope_id") in allowed for fact in facts):
                visible.append(conflict)
        return visible

    def resolve_conflict(
        self, conflict_id: str, *, winner_fact_id: str, decided_by: str
    ) -> dict[str, Any]:
        conflict = self._get_payload("fact_conflicts", conflict_id)
        if not conflict:
            raise KeyError(conflict_id)
        if winner_fact_id not in conflict["fact_ids"]:
            raise ValueError("winner is not part of this conflict")
        now = _now()
        for fact_id in conflict["fact_ids"]:
            fact = self._get_payload("reality_facts", fact_id)
            if not fact:
                continue
            if fact_id == winner_fact_id:
                fact["status"] = "active"
            else:
                fact["status"] = "superseded"
                fact["system_to"] = now
                fact["superseded_by"] = winner_fact_id
            self._save_fact(fact)
        conflict["status"] = "resolved"
        conflict["resolution"] = {
            "winner_fact_id": winner_fact_id,
            "method": "human",
            "decided_by": decided_by,
        }
        conflict["updated_at"] = now
        self._save_conflict(conflict)
        return conflict

    def _record_conflict(
        self,
        proposed: dict[str, Any],
        competing: list[dict[str, Any]],
        *,
        single_valued: bool,
    ) -> dict[str, Any]:
        now = _now()
        conflict = {
            "id": new_id("conflict"),
            "organization_id": proposed["organization_id"],
            "subject_id": proposed["subject_id"],
            "predicate": proposed["predicate"],
            "fact_ids": [proposed["id"], *[item["id"] for item in competing]],
            "objects": [
                proposed["object_value"],
                *[item["object_value"] for item in competing],
            ],
            "kind": "mutually_exclusive" if single_valued else "competing_claims",
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO fact_conflicts
                (id, organization_id, subject_id, predicate, status, payload,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conflict["id"],
                    conflict["organization_id"],
                    conflict["subject_id"],
                    conflict["predicate"],
                    "open",
                    _dump(conflict),
                    now,
                    now,
                ),
            )
        return conflict

    def _save_fact(self, fact: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """UPDATE reality_facts
                SET status = ?, valid_to = ?, system_to = ?, payload = ?
                WHERE id = ?""",
                (
                    fact["status"],
                    fact.get("valid_to"),
                    fact.get("system_to"),
                    _dump(fact),
                    fact["id"],
                ),
            )

    def _save_conflict(self, conflict: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """UPDATE fact_conflicts
                SET status = ?, payload = ?, updated_at = ? WHERE id = ?""",
                (
                    conflict["status"],
                    _dump(conflict),
                    conflict.get("updated_at") or _now(),
                    conflict["id"],
                ),
            )

    def _resolve_claim_subject(
        self, memory: dict[str, Any], subject: str
    ) -> str:
        entities = memory.get("entity_ids") or []
        if len(entities) == 1:
            return entities[0]
        subject_norm = _normalize(subject)
        for entity_id in entities:
            entity = self.store.get_entity(entity_id)
            if entity and subject_norm in {
                _normalize(entity.get("name")),
                *[_normalize(alias) for alias in entity.get("aliases") or []],
            }:
                return entity_id
        return f"claim:{hashlib.sha256(subject_norm.encode()).hexdigest()[:20]}"

    # ------------------------------------------------------------------
    # Cross-source cases and judgment
    # ------------------------------------------------------------------

    def correlate_case(
        self,
        source: dict[str, Any],
        memories: list[dict[str, Any]],
        entity_ids: list[str],
    ) -> dict[str, Any] | None:
        relevant = [
            memory
            for memory in memories
            if memory.get("memory_type") in self.CASE_MEMORY_TYPES
        ]
        if not relevant:
            return None
        tokens = self._case_tokens(source.get("content") or "")
        source_scope = source.get("scope_id") or "scope_org_public"
        candidates = self.list_cases(
            source["organization_id"], statuses={"open", "investigating"}
        )
        best: tuple[float, dict[str, Any]] | None = None
        for case in candidates:
            if case.get("scope_ids") and source_scope not in set(
                case["scope_ids"]
            ):
                continue
            shared_entities = len(
                set(entity_ids) & set(case.get("entity_ids") or [])
            )
            shared_tokens = len(tokens & set(case.get("correlation_tokens") or []))
            score = min(shared_entities * 0.55 + shared_tokens * 0.08, 1.0)
            if score >= 0.45 and (best is None or score > best[0]):
                best = (score, case)
        if best:
            case = best[1]
        else:
            now = _now()
            title = (relevant[0].get("content") or source.get("content") or "Company case")[:160]
            case = {
                "id": new_id("case"),
                "organization_id": source["organization_id"],
                "case_type": relevant[0].get("memory_type") or "work",
                "status": "open",
                "title": title,
                "summary": title,
                "entity_ids": sorted(set(entity_ids)),
                "correlation_tokens": sorted(tokens),
                "source_ids": [],
                "scope_ids": [source_scope],
                "created_at": now,
                "updated_at": now,
            }
            self._insert_case(case)
        if source["id"] not in case["source_ids"]:
            case["source_ids"].append(source["id"])
        case["entity_ids"] = sorted(
            set(case.get("entity_ids") or []) | set(entity_ids)
        )
        case["scope_ids"] = sorted(
            set(case.get("scope_ids") or []) | {source_scope}
        )
        case["correlation_tokens"] = sorted(
            set(case.get("correlation_tokens") or []) | tokens
        )
        case["updated_at"] = _now()
        self._save_case(case)
        event = {
            "id": new_id("case_event"),
            "organization_id": source["organization_id"],
            "case_id": case["id"],
            "source_id": source["id"],
            "occurred_at": source.get("occurred_at"),
            "memory_ids": [memory["id"] for memory in relevant],
            "created_at": _now(),
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO case_events
                (id, organization_id, case_id, source_id, occurred_at, payload,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["id"],
                    event["organization_id"],
                    event["case_id"],
                    event["source_id"],
                    event["occurred_at"],
                    _dump(event),
                    event["created_at"],
                ),
            )
        return case

    def list_cases(
        self,
        organization_id: str,
        statuses: set[str] | None = None,
        scope_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        cases = self._list_payloads(
            "cases", "organization_id = ?", (organization_id,), "updated_at DESC"
        )
        return [
            case
            for case in cases
            if (statuses is None or case["status"] in statuses)
            and (
                scope_ids is None
                or bool(set(case.get("scope_ids") or []) & set(scope_ids))
            )
        ]

    def case_timeline(self, case_id: str) -> list[dict[str, Any]]:
        return self._list_payloads(
            "case_events", "case_id = ?", (case_id,), "occurred_at"
        )

    def update_case(
        self,
        case_id: str,
        *,
        status: str,
        updated_by: str,
        summary: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "open",
            "investigating",
            "blocked",
            "mitigated",
            "resolved",
            "dismissed",
        }
        if status not in allowed:
            raise ValueError(f"unsupported case status: {status}")
        with self.store.cursor() as cur:
            cur.execute("SELECT payload FROM cases WHERE id = ?", (case_id,))
            row = cur.fetchone()
        if not row:
            raise KeyError(case_id)
        case = _load(row["payload"])
        previous = case["status"]
        case["status"] = status
        case["summary"] = summary or case.get("summary")
        case["title"] = title or case["title"]
        case["updated_by"] = updated_by
        case["updated_at"] = _now()
        history = list(case.get("status_history") or [])
        history.append(
            {
                "from": previous,
                "to": status,
                "updated_by": updated_by,
                "at": case["updated_at"],
            }
        )
        case["status_history"] = history
        self._save_case(case)
        self.audit.record(
            case["organization_id"],
            "case.updated",
            {
                "case_id": case_id,
                "from": previous,
                "to": status,
            },
            principal_id=updated_by,
        )
        return case

    def record_judgment(
        self,
        *,
        organization_id: str,
        judgment_type: str,
        principal_id: str | None,
        subject: dict[str, Any],
        decision: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        judgment = {
            "id": new_id("judgment"),
            "organization_id": organization_id,
            "principal_id": principal_id,
            "judgment_type": judgment_type,
            "decision": decision,
            "reason": reason,
            "subject": subject,
            "status": "active",
            "created_at": now,
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO judgment_events
                (id, organization_id, principal_id, judgment_type, status,
                 payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    judgment["id"],
                    organization_id,
                    principal_id,
                    judgment_type,
                    "active",
                    _dump(judgment),
                    now,
                ),
            )
        return judgment

    def list_judgments(
        self,
        organization_id: str,
        *,
        principal_id: str | None = None,
        judgment_type: str | None = None,
    ) -> list[dict[str, Any]]:
        judgments = self._list_payloads(
            "judgment_events",
            "organization_id = ?",
            (organization_id,),
            "created_at DESC",
        )
        return [
            item
            for item in judgments
            if (not principal_id or item.get("principal_id") == principal_id)
            and (not judgment_type or item["judgment_type"] == judgment_type)
        ]

    def judgment_guidance(
        self, organization_id: str, principal_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        values = self.list_judgments(organization_id)
        applicable = [
            item
            for item in values
            if item.get("principal_id") in {None, principal_id}
        ]
        return applicable[:limit]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _insert_case(self, case: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO cases
                (id, organization_id, case_type, status, title, payload,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    case["id"],
                    case["organization_id"],
                    case["case_type"],
                    case["status"],
                    case["title"],
                    _dump(case),
                    case["created_at"],
                    case["updated_at"],
                ),
            )

    def _save_case(self, case: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """UPDATE cases SET status = ?, title = ?, payload = ?,
                                    updated_at = ? WHERE id = ?""",
                (
                    case["status"],
                    case["title"],
                    _dump(case),
                    case["updated_at"],
                    case["id"],
                ),
            )

    @staticmethod
    def _case_tokens(text: str) -> set[str]:
        stop = {
            "about",
            "after",
            "before",
            "could",
            "from",
            "have",
            "into",
            "that",
            "their",
            "there",
            "this",
            "with",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9_-]+", text.lower())
            if len(token) >= 4 and token not in stop
        }

    @staticmethod
    def _intervals_overlap(
        start_a: str | None,
        end_a: str | None,
        start_b: str | None,
        end_b: str | None,
    ) -> bool:
        low_a = start_a or ""
        low_b = start_b or ""
        high_a = end_a or "9999-12-31T23:59:59+00:00"
        high_b = end_b or "9999-12-31T23:59:59+00:00"
        return low_a < high_b and low_b < high_a

    @classmethod
    def _predicate_key(cls, predicate: str) -> str:
        key = re.sub(r"[^a-z0-9]+", "_", _normalize(predicate)).strip("_")
        return cls.PREDICATE_ALIASES.get(key, key)

    def _get_payload(self, table: str, record_id: str) -> dict[str, Any] | None:
        if table not in {"reality_facts", "fact_conflicts"}:
            raise ValueError(table)
        with self.store.cursor() as cur:
            cur.execute(f"SELECT payload FROM {table} WHERE id = ?", (record_id,))
            row = cur.fetchone()
        return _load(row["payload"]) if row else None

    def _fact_has_live_source(self, fact: dict[str, Any]) -> bool:
        source_ids = fact.get("source_ids") or []
        if not source_ids:
            return True
        for source_id in source_ids:
            source = self.store.get_source(source_id)
            if source and not source.get("revoked") and not source.get("deleted_at"):
                return True
        # Explicit API facts may cite external IDs that are not local sources.
        return not any(self.store.get_source(source_id) for source_id in source_ids)

    def _list_payloads(
        self,
        table: str,
        where: str,
        params: tuple[Any, ...],
        order_by: str,
    ) -> list[dict[str, Any]]:
        allowed = {
            "connector_sync_runs",
            "external_identifiers",
            "entity_resolution_candidates",
            "reality_facts",
            "fact_conflicts",
            "cases",
            "case_events",
            "judgment_events",
        }
        if table not in allowed:
            raise ValueError(table)
        with self.store.cursor() as cur:
            cur.execute(
                f"SELECT payload FROM {table} WHERE {where} ORDER BY {order_by}",
                params,
            )
            return [_load(row["payload"]) for row in cur.fetchall()]
