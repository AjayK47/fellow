"""Versioned conditional policy evaluation and action-state revalidation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from toda.domain.models import new_id, utcnow


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _now() -> str:
    return utcnow().isoformat()


class PolicyEngine:
    """Small typed policy engine with default deny and forbid precedence."""

    def __init__(
        self,
        store: Any,
        audit: Any,
        external_reader: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.external_reader = external_reader

    def create_version(
        self,
        *,
        organization_id: str,
        policy_key: str,
        effect: str,
        actions: list[str],
        conditions: dict[str, Any] | None = None,
        approval_required: bool = False,
        precondition: dict[str, Any] | None = None,
        created_by: str,
        status: str = "active",
    ) -> dict[str, Any]:
        if effect not in {"permit", "forbid"}:
            raise ValueError("policy effect must be permit or forbid")
        with self.store.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(MAX(version), 0) AS version
                FROM policy_versions
                WHERE organization_id = ? AND policy_key = ?""",
                (organization_id, policy_key),
            )
            version = int(cur.fetchone()["version"]) + 1
        now = _now()
        record = {
            "id": new_id("policy"),
            "organization_id": organization_id,
            "policy_key": policy_key,
            "version": version,
            "status": status,
            "effect": effect,
            "actions": actions,
            "conditions": conditions or {},
            "approval_required": approval_required,
            "precondition": precondition,
            "created_by": created_by,
            "created_at": now,
        }
        with self.store.cursor() as cur:
            if status == "active":
                cur.execute(
                    """UPDATE policy_versions SET status = 'superseded'
                    WHERE organization_id = ? AND policy_key = ?
                      AND status = 'active'""",
                    (organization_id, policy_key),
                )
            cur.execute(
                """INSERT INTO policy_versions
                (id, organization_id, policy_key, version, status, effect,
                 payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    organization_id,
                    policy_key,
                    version,
                    status,
                    effect,
                    _dump(record),
                    now,
                ),
            )
        self.audit.record(
            organization_id,
            "policy.version.created",
            {
                "policy_id": record["id"],
                "policy_key": policy_key,
                "version": version,
                "status": status,
            },
            principal_id=created_by,
        )
        return record

    def activate(self, policy_id: str, *, approved_by: str) -> dict[str, Any]:
        record = self.get(policy_id)
        if not record:
            raise KeyError(policy_id)
        with self.store.cursor() as cur:
            cur.execute(
                """UPDATE policy_versions SET status = 'superseded'
                WHERE organization_id = ? AND policy_key = ?
                  AND status = 'active'""",
                (record["organization_id"], record["policy_key"]),
            )
            record["status"] = "active"
            record["approved_by"] = approved_by
            record["approved_at"] = _now()
            cur.execute(
                """UPDATE policy_versions SET status = 'active', payload = ?
                WHERE id = ?""",
                (_dump(record), policy_id),
            )
        return record

    def get(self, policy_id: str) -> dict[str, Any] | None:
        with self.store.cursor() as cur:
            cur.execute(
                "SELECT payload FROM policy_versions WHERE id = ?", (policy_id,)
            )
            row = cur.fetchone()
        return json.loads(row["payload"]) if row else None

    def list_versions(
        self, organization_id: str, policy_key: str | None = None
    ) -> list[dict[str, Any]]:
        with self.store.cursor() as cur:
            if policy_key:
                cur.execute(
                    """SELECT status, payload FROM policy_versions
                    WHERE organization_id = ? AND policy_key = ?
                    ORDER BY version DESC""",
                    (organization_id, policy_key),
                )
            else:
                cur.execute(
                    """SELECT status, payload FROM policy_versions
                    WHERE organization_id = ?
                    ORDER BY policy_key, version DESC""",
                    (organization_id,),
                )
            values = []
            for row in cur.fetchall():
                value = json.loads(row["payload"])
                value["status"] = row["status"]
                values.append(value)
            return values

    def evaluate(
        self,
        *,
        organization_id: str,
        action: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        policies = [
            policy
            for policy in self.list_versions(organization_id)
            if policy["status"] == "active"
            and self._action_matches(action, policy.get("actions") or [])
        ]
        matches = [
            policy
            for policy in policies
            if self._conditions_match(policy.get("conditions") or {}, context)
        ]
        forbids = [policy for policy in matches if policy["effect"] == "forbid"]
        permits = [policy for policy in matches if policy["effect"] == "permit"]
        if forbids:
            decision = "deny"
            determining = forbids
        elif permits:
            decision = "allow"
            determining = permits
        elif policies:
            decision = "deny"
            determining = []
        else:
            # Compatibility mode until an organization creates policies for
            # this action. Existing tool policies remain authoritative.
            decision = "legacy"
            determining = []
        return {
            "decision": decision,
            "determining_policy_ids": [item["id"] for item in determining],
            "approval_required": any(
                bool(item.get("approval_required")) for item in determining
            ),
            "preconditions": [
                item["precondition"]
                for item in determining
                if item.get("precondition")
            ],
        }

    def capture_preconditions(
        self,
        *,
        organization_id: str,
        approval_id: str,
        specifications: list[dict[str, Any]],
        runtime: dict[str, Any],
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        captured = []
        for spec in specifications:
            state = self._read_external_state(spec, runtime, arguments)
            now = _now()
            record = {
                "id": new_id("precondition"),
                "organization_id": organization_id,
                "approval_id": approval_id,
                "status": "captured",
                "specification": spec,
                "snapshot": state,
                "snapshot_hash": self._hash_state(state),
                "created_at": now,
                "checked_at": None,
            }
            with self.store.cursor() as cur:
                cur.execute(
                    """INSERT INTO action_preconditions
                    (id, organization_id, approval_id, status, payload,
                     created_at, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record["id"],
                        organization_id,
                        approval_id,
                        "captured",
                        _dump(record),
                        now,
                        None,
                    ),
                )
            captured.append(record)
        return captured

    def revalidate(
        self,
        *,
        approval_id: str,
        runtime: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        records = self._preconditions(approval_id)
        changes = []
        for record in records:
            current = self._read_external_state(
                record["specification"], runtime, arguments
            )
            current_hash = self._hash_state(current)
            passed = current_hash == record["snapshot_hash"]
            record["status"] = "passed" if passed else "changed"
            record["checked_at"] = _now()
            record["current"] = current
            record["current_hash"] = current_hash
            with self.store.cursor() as cur:
                cur.execute(
                    """UPDATE action_preconditions
                    SET status = ?, payload = ?, checked_at = ? WHERE id = ?""",
                    (
                        record["status"],
                        _dump(record),
                        record["checked_at"],
                        record["id"],
                    ),
                )
            if not passed:
                changes.append(record)
        return {
            "valid": not changes,
            "checked": len(records),
            "changes": changes,
        }

    def _preconditions(self, approval_id: str) -> list[dict[str, Any]]:
        with self.store.cursor() as cur:
            cur.execute(
                """SELECT payload FROM action_preconditions
                WHERE approval_id = ? ORDER BY created_at""",
                (approval_id,),
            )
            return [json.loads(row["payload"]) for row in cur.fetchall()]

    def _read_external_state(
        self,
        specification: dict[str, Any],
        runtime: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.external_reader:
            raise RuntimeError("external state reader is not configured")
        read_args = self._render(specification.get("arguments") or {}, arguments)
        return self.external_reader(
            action_slug=specification["action_slug"],
            arguments=read_args,
            connected_account_id=(
                specification.get("connected_account_id")
                or runtime.get("connected_account_id")
            ),
            user_id=runtime.get("composio_user_id"),
        )

    @classmethod
    def _conditions_match(
        cls, conditions: dict[str, Any], context: dict[str, Any]
    ) -> bool:
        for path, expected in conditions.items():
            actual = cls._path(context, path)
            if isinstance(expected, dict):
                if "in" in expected and actual not in expected["in"]:
                    return False
                if "not_in" in expected and actual in expected["not_in"]:
                    return False
                if "equals" in expected and actual != expected["equals"]:
                    return False
                if "max" in expected and (
                    actual is None or float(actual) > float(expected["max"])
                ):
                    return False
                if "min" in expected and (
                    actual is None or float(actual) < float(expected["min"])
                ):
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _action_matches(action: str, patterns: list[str]) -> bool:
        return any(
            pattern == "*"
            or pattern == action
            or (
                pattern.endswith("*")
                and action.startswith(pattern[:-1])
            )
            for pattern in patterns
        )

    @staticmethod
    def _path(value: dict[str, Any], path: str) -> Any:
        current: Any = value
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @classmethod
    def _render(cls, value: Any, arguments: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: cls._render(item, arguments) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._render(item, arguments) for item in value]
        if isinstance(value, str) and value.startswith("$args."):
            return cls._path(arguments, value[6:])
        return value

    @staticmethod
    def _hash_state(state: dict[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(_dump(state).encode()).hexdigest()
