"""Company reality engine: canonical objects, identity, temporal facts, cases, and judgment."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from toda.domain.models import new_id, utcnow

log = logging.getLogger("toda.reality")


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _load(value: str) -> dict[str, Any]:
    return json.loads(value)


def _now() -> str:
    return utcnow().isoformat()


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


# Entity-resolution safety gate, ported from Graphiti's dedup_helpers entropy
# check (github.com/getzep/graphiti): a name that is both short and low in
# character variety ("Al", "Jo", "the app") is exactly where a fuzzy-ratio
# score produces confident-looking false-positive merges. Below this bar,
# resolution should create a new entity or queue a review candidate instead
# of trusting the fuzzy match — see `_is_resolvable_label`.
_MIN_LABEL_LENGTH = 6
_MIN_LABEL_TOKENS = 2
_LABEL_ENTROPY_THRESHOLD = 1.5


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _is_resolvable_label(label: str) -> bool:
    """Long/multi-word labels are trusted directly; short ones only pass if
    they're character-diverse enough that a fuzzy match means something."""
    if len(label) >= _MIN_LABEL_LENGTH and len(label.split()) >= _MIN_LABEL_TOKENS:
        return True
    return _shannon_entropy(label) >= _LABEL_ENTROPY_THRESHOLD


class RealityEngine:
    """Evidence-first materialization of the current and historical company state."""

    # Authorization edges stay a fixed system vocabulary, written only by
    # admin APIs — never by extraction. A claim or projection using one of
    # these names is refused and logged.
    RESERVED_EDGE_TYPES = frozenset(
        {
            "MEMBER_OF",
            "CAN_VIEW",
            "CAN_EDIT",
            "VISIBLE_TO",
            "ACTS_AS",
            "AUTHORIZED_FOR",
            "INHERITS_ACL_FROM",
        }
    )
    CASE_MEMORY_TYPES = {"issue", "commitment", "decision", "outcome"}
    # Cardinality for claims no model judged (the offline extractor): these
    # hold one value at a time, everything else the catalog hasn't learned
    # as single may hold many.
    UNJUDGED_SINGLE_VALUED = frozenset(
        {"owner", "assignee", "status", "state", "priority", "launch_date", "release_date", "on_call"}
    )

    def __init__(
        self, store: Any, graph: Any, audit: Any, embeddings: Any | None = None
    ) -> None:
        self.store = store
        self.graph = graph
        self.audit = audit
        self.embeddings = embeddings

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

    def link_identifier_or_propose(
        self,
        *,
        organization_id: str,
        entity_id: str,
        namespace: str,
        value: str,
        confidence: float,
        verified: bool,
        source_id: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any] | None:
        """Link an identifier, or file a resolution candidate when it already
        belongs to somebody else.

        `link_identifier` raises on a conflict, which is right for a
        deliberate operator action and wrong for automated ingestion: two
        systems disagreeing about who owns an address is an ordinary
        ambiguity, and raising through the ingest path would drop the event
        entirely over a question a human can answer later. Returning None
        means "not linked, a candidate is pending" -- never "no conflict".
        """
        existing = self.lookup_identifier(organization_id, namespace, value)
        if existing and existing["entity_id"] != entity_id:
            entity = self.graph.get_entity(entity_id)
            if entity:
                self._create_resolution_candidate(
                    organization_id,
                    entity,
                    {
                        "namespace": namespace,
                        "value": value,
                        "label": label or value,
                        "strength": "soft",
                    },
                    source_id or "",
                )
            return None
        return self.link_identifier(
            organization_id=organization_id,
            entity_id=entity_id,
            namespace=namespace,
            value=value,
            confidence=confidence,
            verified=verified,
            source_id=source_id,
        )

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
        entity, _is_exact = self._match_entity(organization_id, identifier)
        return entity

    def _match_entity(
        self, organization_id: str, identifier: dict[str, str]
    ) -> tuple[dict[str, Any] | None, bool]:
        """Same lookup _candidate_entity always did, but also reports
        whether the match was exact -- resolve_source_entities doesn't
        care (either way it has an entity to link to), but
        propose_entity_mention does: an exact match is safe to auto-link,
        a merely-similar one is not (it might be a different real entity
        with a similar name) and must go through human review instead."""
        label = _normalize(identifier.get("label"))
        value = _normalize(identifier["value"])
        if identifier["namespace"] == "email":
            local = value.split("@", 1)[0].replace(".", " ").replace("_", " ")
            label = local
        entities = self.store.list_entities(organization_id)
        # Fast path: an exact normalized name/alias match is unambiguous —
        # no need to trust a fuzzy score for it, and it can't be beaten by
        # a merely-similar name on a different entity.
        for entity in entities:
            candidates = [entity.get("name") or "", *(entity.get("aliases") or [])]
            if any(_normalize(name) == label for name in candidates if name):
                return entity, True
        if not _is_resolvable_label(label):
            # Too short/generic for a fuzzy ratio to mean anything — the
            # caller (resolve_source_entities) treats "no match" as reason
            # to create a new entity or queue a review candidate rather than
            # silently merging into whichever existing entity happens to
            # score highest on a near-meaningless comparison.
            return None, False
        best: tuple[float, dict[str, Any]] | None = None
        for entity in entities:
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
        return (best[1], False) if best else (None, False)

    def propose_entity_mention(
        self,
        organization_id: str,
        *,
        name: str,
        entity_type: str,
        source_id: str,
        aliases: list[str] | None = None,
    ) -> tuple[str | None, str]:
        """Resolve a plain-text entity MENTION -- a name the model found in
        free-form conversation, as opposed to a structured connector
        payload's identifier -- against the existing catalog.

        This is the missing half of graph population: resolve_source_entities
        only ever creates a business entity (team/project/customer/...) from
        a "hard" identifier in a connector's structured data (an email, a
        customer_id). An ordinary Slack conversation has no such structured
        payload, so before this existed the graph could only ever grow
        person/conversation/source/task entities -- every project, team,
        customer and incident anyone ever talked about was invisible to it.

        Follows resolve_source_entities' own conservative policy for a name
        alone (no unique key backing it) as closely as possible, reusing the
        exact same matching logic via _match_entity:
          - an exact name/alias match is trusted directly (safe: two
            identical normalized names are the same real thing);
          - a merely-similar match is NOT auto-linked -- it queues a
            pending resolution candidate for a human to confirm via the
            existing /v1/entity-resolution-candidates review flow instead,
            the same as an ambiguous "soft" identifier match does;
          - the one deliberate deviation from that policy: no match at all
            still creates a new entity, rather than doing nothing. Doing
            nothing is exactly what left the graph permanently empty of
            every business entity type -- a mention in conversation is the
            only signal chat-based extraction ever gets, so refusing to
            create from it means nothing new ever enters the graph at all.

        Returns (entity_id, decision) where decision is "matched",
        "created", or "candidate" (entity_id is None only for "candidate" --
        the caller must not link a memory to an entity that doesn't exist
        yet, pending or not).
        """
        identifier = {
            "namespace": "name_mention",
            "value": name,
            "label": name,
        }
        # Check for an already-confirmed link FIRST, exactly like
        # resolve_source_entities does for a structured identifier. This is
        # what makes review actually mean something: decide_resolution_
        # candidate's approve path calls link_identifier on this same
        # (namespace, value) pair -- without this check up front, the next
        # mention of the identical name would just fuzzy-match and queue a
        # SECOND candidate for the same already-resolved question forever.
        existing_link = self.lookup_identifier(
            organization_id, identifier["namespace"], _normalize(name)
        )
        if existing_link:
            return existing_link["entity_id"], "matched"
        entity, is_exact = self._match_entity(organization_id, identifier)
        if entity and is_exact:
            return entity["id"], "matched"
        if entity:
            self._create_resolution_candidate(
                organization_id,
                entity,
                {**identifier, "strength": "soft"},
                source_id,
            )
            return None, "candidate"
        entity_id = new_id("entity")
        self.graph.upsert_entity(
            {
                "id": entity_id,
                "organization_id": organization_id,
                "entity_type": entity_type,
                "name": name,
                "aliases": aliases or [],
                "attributes": {
                    "created_by_extraction": True,
                    "source_id": source_id,
                },
            }
        )
        return entity_id, "created"

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
        if (memory.get("memory_type") or "") in {"derived", "outcome"}:
            return None
        claim = memory.get("claim")
        if isinstance(claim, dict) and claim.get("decision"):
            return self.apply_claim(
                memory["organization_id"], claim, memory, source=source
            )
        claim = memory.get("canonical_claim") or {}
        if not all(claim.get(key) for key in ("subject", "predicate", "object")):
            return None
        subject_id = self._resolve_claim_subject(memory, claim["subject"])
        return self.apply_claim(
            memory["organization_id"],
            {
                "subject_id": subject_id,
                "predicate": claim["predicate"],
                "object_value": claim["object"],
            },
            memory,
            source=source,
        )

    # ------------------------------------------------------------------
    # Predicate catalog and update decisions
    # ------------------------------------------------------------------

    def normalize_predicate(self, organization_id: str, raw: str) -> str:
        """Snake_case, reuse a catalog predicate on cosine >= 0.88, else create."""
        from toda.domain.models import new_id as _new_id, utcnow as _utcnow

        key = self._predicate_key(raw)
        self.store.seed_predicates_from_facts(organization_id)
        existing = self.store.get_predicate(organization_id, key)
        now = _utcnow().isoformat()
        if existing is not None:
            existing["usage_count"] = int(existing.get("usage_count") or 0) + 1
            existing["updated_at"] = now
            self.store.upsert_predicate(existing)
            return existing["name"]
        embedding = self._embed_text(raw)
        if embedding:
            best: dict[str, Any] | None = None
            best_score = 0.0
            for candidate in self.store.predicates_by_usage(organization_id, limit=200):
                candidate_embedding = candidate.get("embedding")
                if not candidate_embedding:
                    continue
                score = _cosine(embedding, candidate_embedding)
                if score > best_score:
                    best_score, best = score, candidate
            if best is not None and best_score >= 0.88:
                best["usage_count"] = int(best.get("usage_count") or 0) + 1
                best["updated_at"] = now
                self.store.upsert_predicate(best)
                return best["name"]
        self.store.upsert_predicate(
            {
                "id": _new_id("predicate"),
                "organization_id": organization_id,
                "name": key,
                "description": "",
                "examples": [raw] if raw != key else [],
                "usage_count": 1,
                "cardinality": "unknown",
                "embedding": embedding,
                "created_at": now,
                "updated_at": now,
            }
        )
        return key

    def _embed_text(self, text: str) -> list[float] | None:
        if self.embeddings is None:
            return None
        try:
            vectors = self.embeddings.embed([text])
        except Exception:
            return None
        if not vectors or not vectors[0]:
            return None
        return [float(v) for v in vectors[0]]

    def _set_cardinality(self, organization_id: str, predicate: str, cardinality: str) -> None:
        from toda.domain.models import utcnow as _utcnow

        record = self.store.get_predicate(organization_id, predicate)
        if record is None or record.get("cardinality") != "unknown":
            return
        record["cardinality"] = cardinality
        record["updated_at"] = _utcnow().isoformat()
        self.store.upsert_predicate(record)

    def visible_scope_ids(self, organization_id: str, scope_id: str) -> set[str]:
        """Scopes a source in `scope_id` may read: its own plus public ones."""
        visible = {scope_id} if scope_id else set()
        for scope in self.store.list_scopes(organization_id):
            if scope.get("is_public"):
                visible.add(scope["id"])
        return visible

    def apply_claim(
        self,
        organization_id: str,
        claim: dict[str, Any],
        memory: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Apply an extractor update decision. Returns the fact, if any."""
        from toda.domain.models import utcnow as _utcnow

        decision = str(claim.get("decision") or "ADD").upper()
        if decision not in {"ADD", "UPDATE", "CONTRADICTS", "NOOP"}:
            return None
        predicate = self.normalize_predicate(organization_id, str(claim.get("predicate") or ""))
        if not predicate:
            return None
        if self._refuse_reserved(predicate):
            return None
        memory_type = str(memory.get("memory_type") or "")
        scope_id = str(memory.get("scope_id") or "")
        if memory_type == "preference":
            # Preferences live in the speaker's personal scope even when
            # stated in a channel. The claim subject is the speaker.
            speaker = str(
                memory.get("author_entity_id")
                or claim.get("subject_id")
                or ""
            )
            personal_scope = f"scope_person_{speaker}" if speaker else ""
            if not personal_scope or self.store.get_scope(personal_scope) is None:
                log.info("Skipping preference fact: no personal scope for %s", speaker)
                return None
            scope_id = personal_scope
        subject_id = str(
            claim.get("subject_id")
            or self._resolve_claim_subject(memory, str(claim.get("subject") or ""))
        )
        object_entity_id = claim.get("object_entity_id") or memory.get("object_entity_id")
        object_value = claim.get("object_value")
        if object_value is None and object_entity_id:
            entity = self.store.get_entity(str(object_entity_id))
            object_value = (entity or {}).get("name") or str(object_entity_id)
        if object_value is None:
            return None
        now = _utcnow().isoformat()
        valid_from = (
            memory.get("valid_from")
            or (source or {}).get("occurred_at")
            or now
        )
        source_ids = list(memory.get("source_ids") or [])
        confidence = float(memory.get("confidence") or 0.5)
        authority = float(memory.get("authority") or 0.5)
        # A claim may only see or change facts its source's room can see:
        # its own scope plus public ones. The target id comes from model
        # output over untrusted text, so it is checked, never trusted.
        visible = self.visible_scope_ids(organization_id, str(memory.get("scope_id") or ""))
        target_fact_id = claim.get("target_fact_id")
        target = self._get_payload("reality_facts", str(target_fact_id)) if target_fact_id else None
        if target is not None and not (
            target.get("organization_id") == organization_id
            and target.get("scope_id") in visible
            and target.get("subject_id") == subject_id
            and target.get("predicate") == predicate
        ):
            log.warning("Ignoring claim target outside the claim's reach: %s", target_fact_id)
            target = None

        if decision == "NOOP":
            if target is not None:
                merged = sorted(set(target.get("source_ids") or []) | set(source_ids))
                target["source_ids"] = merged
                self._save_fact(target)
            return None

        if decision == "UPDATE":
            if target is None:
                candidates = self.current_facts(
                    organization_id,
                    subject_id=subject_id,
                    predicate=predicate,
                    scope_ids=sorted(visible),
                )
                target = candidates[0] if candidates else None
            if target is None:
                decision = "ADD"
            else:
                target["valid_to"] = valid_from
                target["status"] = "superseded"
                target["system_to"] = now
                new_fact = {
                    "id": new_id("fact"),
                    "organization_id": organization_id,
                    "subject_id": subject_id,
                    "predicate": predicate,
                    "object_value": object_value,
                    "status": "active",
                    "valid_from": valid_from,
                    "valid_to": memory.get("valid_until"),
                    "system_from": now,
                    "system_to": None,
                    "scope_id": scope_id,
                    "source_ids": source_ids,
                    "memory_id": memory["id"],
                    "confidence": confidence,
                    "authority": authority,
                    "supersedes": target["id"],
                }
                if object_entity_id:
                    new_fact["object_entity_id"] = object_entity_id
                target["superseded_by"] = new_fact["id"]
                self._save_fact(target)
                self._close_projected_edge(organization_id, target["id"], valid_until=valid_from)
                self._set_cardinality(organization_id, predicate, "single")
                with self.store.cursor() as cur:
                    cur.execute(
                        """INSERT INTO reality_facts
                        (id, organization_id, subject_id, predicate, object_value, object_entity_id, status,
                         valid_from, valid_to, system_from, system_to, scope_id, payload)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            new_fact["id"], organization_id, subject_id, predicate,
                            object_value, object_entity_id, "active", valid_from,
                            memory.get("valid_until"), now, None,
                            scope_id, _dump(new_fact),
                        ),
                    )
                self._project_edge(new_fact)
                return new_fact

        if decision == "CONTRADICTS":
            competing = self.current_facts(
                organization_id,
                subject_id=subject_id,
                predicate=predicate,
                scope_ids=sorted(visible),
            )
            if target is not None and all(f["id"] != target["id"] for f in competing):
                competing = [target, *competing]
            fact = {
                "id": new_id("fact"),
                "organization_id": organization_id,
                "subject_id": subject_id,
                "predicate": predicate,
                "object_value": object_value,
                "status": "contested",
                "valid_from": valid_from,
                "valid_to": memory.get("valid_until"),
                "system_from": now,
                "system_to": None,
                "scope_id": scope_id,
                "source_ids": source_ids,
                "memory_id": memory["id"],
                "confidence": confidence,
                "authority": authority,
            }
            if object_entity_id:
                fact["object_entity_id"] = object_entity_id
            with self.store.cursor() as cur:
                cur.execute(
                    """INSERT INTO reality_facts
                    (id, organization_id, subject_id, predicate, object_value, object_entity_id, status,
                     valid_from, valid_to, system_from, system_to, scope_id, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        fact["id"], organization_id, subject_id, predicate,
                        object_value, object_entity_id, "contested", valid_from,
                        memory.get("valid_until"), now, None,
                        scope_id, _dump(fact),
                    ),
                )
            conflict = self._record_conflict(fact, competing, single_valued=False)
            fact["conflict_id"] = conflict["id"]
            return fact

        # ADD: a model that saw the live values and still chose ADD teaches
        # the catalog the predicate holds many. A claim nobody judged (no
        # decision) teaches nothing and supersedes only single predicates.
        judged = bool(claim.get("decision"))
        if judged:
            live = [
                fact
                for fact in self.current_facts(
                    organization_id, subject_id=subject_id, predicate=predicate
                )
                if _normalize(fact.get("object_value")) != _normalize(object_value)
            ]
            if live:
                self._set_cardinality(organization_id, predicate, "multi")
        return self.assert_fact(
            organization_id=organization_id,
            subject_id=subject_id,
            predicate=predicate,
            object_value=object_value,
            scope_id=scope_id,
            valid_from=valid_from,
            valid_to=memory.get("valid_until"),
            source_ids=source_ids,
            memory_id=memory["id"],
            confidence=confidence,
            authority=authority,
            object_entity_id=object_entity_id,
            judged=judged,
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
        object_entity_id: str | None = None,
        judged: bool = True,
    ) -> dict[str, Any]:
        predicate_key = self._predicate_key(predicate)
        if self._refuse_reserved(predicate_key):
            raise ValueError(f"refused reserved predicate: {predicate_key}")
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
            "object_entity_id": object_entity_id,
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
        single = self._is_single_valued(organization_id, predicate_key, judged=judged)
        if competing and single:
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
                        self._close_projected_edge(organization_id, old["id"], valid_until=valid_from)
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
                (id, organization_id, subject_id, predicate, object_value, object_entity_id, status,
                 valid_from, valid_to, system_from, system_to, scope_id, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fact["id"],
                    organization_id,
                    subject_id,
                    predicate_key,
                    object_value,
                    object_entity_id,
                    fact["status"],
                    valid_from,
                    valid_to,
                    now,
                    None,
                    scope_id,
                    _dump(fact),
                ),
            )
        self._project_edge(fact)
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
        reinforcement_key = self._judgment_reinforcement_key(
            judgment_type, decision, subject
        )
        if reinforcement_key:
            existing = self._find_reinforceable_judgment(
                organization_id, principal_id, reinforcement_key
            )
            if existing:
                existing["reinforcement_count"] = (
                    int(existing.get("reinforcement_count", 1)) + 1
                )
                existing["last_reinforced_at"] = now
                if reason:
                    existing["reason"] = reason
                existing["subject"] = subject
                self._save_judgment(existing)
                self._embed_judgment(existing)
                return existing
        judgment = {
            "id": new_id("judgment"),
            "organization_id": organization_id,
            "principal_id": principal_id,
            "judgment_type": judgment_type,
            "decision": decision,
            "reason": reason,
            "subject": subject,
            "reinforcement_key": reinforcement_key,
            "reinforcement_count": 1,
            "last_reinforced_at": now,
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
        self._embed_judgment(judgment)
        return judgment

    @staticmethod
    def _judgment_reinforcement_key(
        judgment_type: str, decision: str, subject: dict[str, Any]
    ) -> str | None:
        """Group repeated corrections on the same anchor so they reinforce
        one guidance record instead of accumulating as unrelated entries.

        Only fires when the subject names something stable across
        occurrences (a tool, entity, or capability) — call sites like
        ``tool_approval`` already include ``tool_name``; open-ended
        subjects (e.g. a bare ``run_id``) are left alone rather than
        guessed at.
        """
        anchor = (
            subject.get("tool_name")
            or subject.get("entity_id")
            or subject.get("capability_id")
        )
        if not anchor:
            return None
        return f"{judgment_type}:{decision}:{anchor}"

    def _find_reinforceable_judgment(
        self, organization_id: str, principal_id: str | None, reinforcement_key: str
    ) -> dict[str, Any] | None:
        for item in self._list_payloads(
            "judgment_events",
            "organization_id = ?",
            (organization_id,),
            "created_at DESC",
        ):
            if item.get("status") != "active":
                continue
            if item.get("principal_id") != principal_id:
                continue
            if item.get("reinforcement_key") == reinforcement_key:
                return item
        return None

    def _save_judgment(self, judgment: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                "UPDATE judgment_events SET payload = ? WHERE id = ?",
                (_dump(judgment), judgment["id"]),
            )

    def _embed_judgment(self, judgment: dict[str, Any]) -> None:
        if not self.embeddings:
            return
        subject_text = " ".join(
            str(value) for value in (judgment.get("subject") or {}).values()
        )
        text = " ".join(
            [
                judgment.get("judgment_type") or "",
                judgment.get("decision") or "",
                judgment.get("reason") or "",
                subject_text,
            ]
        ).strip()
        if not text:
            return
        vector = self.embeddings.embed([text])[0]
        self.store.upsert_judgment_embedding(
            judgment_id=judgment["id"],
            organization_id=judgment["organization_id"],
            model=self.embeddings.model,
            embedding=vector,
        )

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
        self,
        organization_id: str,
        principal_id: str,
        *,
        limit: int = 10,
        query: str | None = None,
        query_vector: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Rank past corrections by relevance to `query`, not insertion order.

        Combines semantic similarity (when an embedding provider and query
        are available), lexical token overlap, a bounded reinforcement bonus
        for corrections that keep recurring on the same anchor (see
        `_judgment_reinforcement_key`), and a recency decay so a judgment
        nobody has reinforced in months fades rather than staying pinned at
        the top forever. Without a query, ranks on reinforcement + recency
        only — still an improvement over the previous plain insertion order.
        """
        applicable = [
            item
            for item in self.list_judgments(organization_id)
            if item.get("principal_id") in {None, principal_id}
        ]
        if not applicable:
            return []
        query_tokens = self._case_tokens(query) if query else set()
        query_vector = (
            query_vector
            if query_vector
            else (
                self.embeddings.embed([query])[0]
                if query and query.strip() and self.embeddings
                else []
            )
        )
        embeddings = (
            self.store.judgment_embeddings([item["id"] for item in applicable])
            if query_vector
            else {}
        )
        now = utcnow()
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in applicable:
            reinforcement_bonus = (
                min(int(item.get("reinforcement_count") or 1), 5) * 0.04
            )
            age_days = self._judgment_age_days(
                item.get("last_reinforced_at") or item.get("created_at"), now
            )
            recency_bonus = 0.5 ** (age_days / 30.0) * 0.15
            if query_tokens or query_vector:
                text = " ".join(
                    [
                        item.get("reason") or "",
                        " ".join(
                            str(value)
                            for value in (item.get("subject") or {}).values()
                        ),
                    ]
                )
                lexical = (
                    len(query_tokens & self._case_tokens(text))
                    / max(len(query_tokens), 1)
                    if query_tokens
                    else 0.0
                )
                memory_vector = embeddings.get(item["id"]) or []
                semantic = (
                    self._cosine(query_vector, memory_vector)
                    if query_vector and memory_vector
                    else 0.0
                )
                score = (
                    semantic * 0.5
                    + lexical * 0.25
                    + reinforcement_bonus
                    + recency_bonus
                )
            else:
                score = reinforcement_bonus + recency_bonus
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    @staticmethod
    def _judgment_age_days(timestamp: str | None, now: datetime) -> float:
        if not timestamp:
            return 365.0
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return 365.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max((now - parsed).total_seconds() / 86400.0, 0.0)

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(
            sum(v * v for v in right)
        )
        return (
            sum(a * b for a, b in zip(left, right)) / denominator
            if denominator
            else 0.0
        )

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

    def _is_single_valued(
        self, organization_id: str, predicate_key: str, *, judged: bool = True
    ) -> bool:
        """Cardinality comes from the learned catalog. For judged claims an
        unknown predicate defaults to single-valued (conservative: competing
        claims surface as conflicts until the catalog learns otherwise).
        Unjudged claims default to many unless single by nature."""
        try:
            record = self.store.get_predicate(organization_id, predicate_key)
        except Exception:
            record = None
        cardinality = (record or {}).get("cardinality") or "unknown"
        if cardinality != "unknown":
            return cardinality == "single"
        return judged or predicate_key in self.UNJUDGED_SINGLE_VALUED

    def _refuse_reserved(self, predicate: str) -> bool:
        """True when a predicate collides with the authorization vocabulary."""
        if str(predicate or "").upper() in self.RESERVED_EDGE_TYPES:
            log.warning("Refusing knowledge predicate in authorization vocabulary: %s", predicate)
            return True
        return False

    def _project_edge(self, fact: dict[str, Any]) -> dict[str, Any] | None:
        """An edge is a fact whose object is an entity. The graph view is
        derived from facts; traversal keeps reading `edges` unchanged."""
        object_entity_id = fact.get("object_entity_id")
        if not object_entity_id:
            return None
        if self._refuse_reserved(fact.get("predicate") or ""):
            return None
        return self.graph.add_edge(
            fact["organization_id"],
            str(fact["predicate"]),
            str(fact["subject_id"]),
            str(object_entity_id),
            properties={"fact_id": fact["id"]},
            source_ids=list(fact.get("source_ids") or []),
        )

    def _close_projected_edge(self, organization_id: str, fact_id: str, *, valid_until: str | None = None) -> None:
        for edge in self.store.list_edges(organization_id, active_only=False):
            if (edge.get("properties") or {}).get("fact_id") == fact_id and not edge.get("valid_until"):
                self.graph.supersede_edge(edge["id"], valid_until=valid_until)
                return

    @classmethod
    def _predicate_key(cls, predicate: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", _normalize(predicate)).strip("_")

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
