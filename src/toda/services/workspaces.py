"""Durable, per-Slack-workspace workspace metadata.

The filesystem is still the working surface; this service owns the Toda
record that binds it to an organization, a Slack workspace (or an
organization-level default when a Task has no Slack destination), a
principal, a scope, and a quota. Tools must never accept an arbitrary host
path in place of this record.

One durable folder is shared across every channel/conversation in a Slack team.
Agent execution never writes there directly: each child receives a separate
task root under the agent workspace root. Those disposable roots are cleaned
by the recovery heartbeat only after they are inactive and outside the retention
window; the durable workspace itself still requires an explicit org-level
retention decision (see ``expire()``).
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from toda.domain.models import utcnow


class WorkspaceService:
    _PRIVATE_COPY_NAMES = frozenset(
        {
            ".git",
            ".toda-tasks",
            ".trash",
            ".pi",
            ".agent-home",
            "__pycache__",
            ".env",
            ".aws",
            ".ssh",
            ".npmrc",
            ".pypirc",
            ".docker",
            "credentials",
            "secrets",
            "config.json",
            "brain.db",
            "brain.db-wal",
            "brain.db-shm",
        }
    )

    def __init__(
        self,
        store: Any,
        audit: Any,
        root: Path | str,
        *,
        agent_root: Path | str | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        raw_root = Path(root).expanduser()
        if raw_root.is_symlink():
            raise ValueError("durable workspace root cannot be a symlink")
        self.root = raw_root.resolve()
        default_agent_root_raw = (
            self.root.parent.parent / f"{self.root.parent.name}-agent-workspaces"
        )
        raw_agent_root = (
            Path(agent_root).expanduser()
            if agent_root is not None
            else default_agent_root_raw
        )
        if raw_agent_root.is_symlink():
            raise ValueError("agent workspace root cannot be a symlink")
        self.agent_root = (
            raw_agent_root.resolve()
        )
        if self.agent_root == self.root or self.agent_root in self.root.parents or self.root in self.agent_root.parents:
            raise ValueError("agent workspace root must be separate from durable workspace root")

    @staticmethod
    def _folder_key(organization_id: str, slack_workspace_id: str | None) -> str:
        return slack_workspace_id or f"org-{organization_id}-default"

    @staticmethod
    def _validate_component(value: str, label: str) -> str:
        value = str(value or "").strip()
        if not value or value in {".", ".."} or any(char in value for char in "/\\"):
            raise ValueError(f"invalid {label}")
        return value

    def _record_id(self, organization_id: str, slack_workspace_id: str | None) -> str:
        return f"workspace:{organization_id}:{self._folder_key(organization_id, slack_workspace_id)}"

    def get_or_create(
        self,
        *,
        organization_id: str,
        owner_id: str,
        scope_id: str,
        slack_workspace_id: str | None = None,
        quota_bytes: int = 10_000_000,
    ) -> dict[str, Any]:
        if quota_bytes < 1_024 or quota_bytes > 1_000_000_000:
            raise ValueError("workspace quota must be between 1KB and 1GB")
        self._validate_component(organization_id, "organization id")
        if slack_workspace_id is not None:
            self._validate_component(slack_workspace_id, "Slack workspace id")
        scope = self.store.get_scope(scope_id)
        if not scope or scope.get("organization_id") != organization_id:
            raise ValueError("workspace scope does not belong to organization")

        record_id = self._record_id(organization_id, slack_workspace_id)
        existing = self.store.get_harness_record("workspaces", record_id)
        if existing and existing.get("organization_id") == organization_id:
            return existing

        now = utcnow()
        folder_key = self._folder_key(organization_id, slack_workspace_id)
        self._validate_component(folder_key, "workspace folder")
        durable_parent_raw = self.root / ".toda-workspaces"
        if durable_parent_raw.is_symlink():
            raise ValueError("durable workspace boundary cannot be a symlink")
        folder_raw = durable_parent_raw / folder_key
        if folder_raw.is_symlink():
            raise ValueError("durable workspace folder cannot be a symlink")
        record = {
            "id": record_id,
            "organization_id": organization_id,
            "owner_id": owner_id,
            "scope_id": scope_id,
            "slack_workspace_id": slack_workspace_id,
            "path": str(folder_raw.resolve()),
            "quota_bytes": quota_bytes,
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        durable_parent_raw.mkdir(parents=True, exist_ok=True)
        if durable_parent_raw.is_symlink():
            raise ValueError("durable workspace boundary cannot be a symlink")
        folder_raw.mkdir(parents=True, exist_ok=True)
        if folder_raw.is_symlink():
            raise ValueError("durable workspace folder cannot be a symlink")
        self.store.upsert_harness_projection("workspaces", record)
        self.audit.record(
            organization_id,
            "workspace.created",
            {
                "workspace_id": record_id,
                "scope_id": scope_id,
                "slack_workspace_id": slack_workspace_id,
            },
            principal_id=owner_id,
        )
        return record

    def get_for_task(
        self, organization_id: str, task: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Resolve the persistent workspace folder for a Task.

        A Task carries its Slack workspace id (if any) at
        ``task["metadata"]["slack"]["workspace_id"]``. Tasks sharing that id
        resolve to the same folder regardless of which channel/conversation
        triggered them; a Task with no Slack destination falls back to the
        organization's default folder.
        """
        slack_workspace_id = ((task or {}).get("metadata") or {}).get("slack", {}).get(
            "workspace_id"
        )
        record = self.store.get_harness_record(
            "workspaces", self._record_id(organization_id, slack_workspace_id)
        )
        if record and record.get("organization_id") == organization_id:
            return record
        return None

    def resolve_durable_path(self, record: dict[str, Any]) -> Path:
        """Validate a persisted workspace path before any filesystem use."""
        parent_raw = self.root / ".toda-workspaces"
        if parent_raw.is_symlink():
            raise ValueError("durable workspace boundary cannot be a symlink")
        parent = parent_raw.resolve()
        candidate = Path(str(record.get("path") or "")).expanduser().resolve()
        organization_id = self._validate_component(
            str(record.get("organization_id") or ""), "organization id"
        )
        slack_workspace_id = record.get("slack_workspace_id")
        if slack_workspace_id is not None:
            slack_workspace_id = self._validate_component(
                str(slack_workspace_id), "Slack workspace id"
            )
        expected = parent / self._folder_key(
            organization_id,
            slack_workspace_id,
        )
        self._validate_component(expected.name, "workspace folder")
        if candidate.parent != parent or candidate == parent or candidate != expected:
            raise ValueError("workspace record points outside Toda's durable workspace root")
        return candidate

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_harness_records(
            "workspaces", organization_id, order_by="updated_at", descending=True
        )

    def task_root(self, organization_id: str, task: dict[str, Any]) -> Path:
        """Return an agent-owned, task-specific working directory.

        The durable workspace remains the organization/shared repository. A
        coding Task never receives that directory directly: it gets a
        disposable root under the configured agent root so parallel workers cannot
        overwrite one another's files.
        """
        record = self.get_for_task(organization_id, task)
        if not record or record.get("status") != "active":
            raise ValueError("no active workspace resolved for task")
        task_id = self._validate_component(str(task.get("id") or ""), "task id")
        # Task worktrees intentionally live outside the durable workspace.
        # This lets an operator make the durable tree/control plane private to
        # Toda while granting the low-privilege agent account access only to
        # disposable task material.
        org_key = self._validate_component(organization_id, "organization id")
        org_root = self.agent_root / org_key
        if org_root.is_symlink():
            raise ValueError("organization workspace boundary cannot be a symlink")
        org_root.mkdir(parents=True, exist_ok=True)
        if org_root.is_symlink():
            raise ValueError("organization workspace boundary cannot be a symlink")
        raw_task_root = org_root / task_id
        # Do not accept a task path that aliases another task, even when the
        # alias target remains inside the agent root.  The raw and resolved
        # paths must be the same directory owned by this Task.
        if raw_task_root.is_symlink():
            raise ValueError("task workspace cannot be a symlink")
        task_root = raw_task_root.resolve()
        if (
            self.agent_root not in task_root.parents
            or task_root.parent != org_root.resolve()
            or task_root != raw_task_root
        ):
            raise ValueError("task workspace escaped its agent root")
        task_root.mkdir(parents=True, exist_ok=True)
        return task_root

    def prepare_task_workspace(
        self,
        organization_id: str,
        task: dict[str, Any],
        *,
        base_revision: str = "HEAD",
        isolated: bool = False,
    ) -> tuple[Path, str]:
        """Create a private Git worktree or a directory copy for one Task."""
        durable = self.get_for_task(organization_id, task)
        if not durable or durable.get("status") != "active":
            raise ValueError("no active workspace resolved for task")
        if str(task.get("organization_id") or organization_id) != organization_id:
            raise ValueError("task does not belong to organization")
        durable_root = self.resolve_durable_path(durable)
        task_root = self.task_root(organization_id, task)
        task_metadata = dict(task.get("metadata") or {})
        task_root_empty = not any(task_root.iterdir())
        if "workspace_base_snapshot" not in task_metadata or task_root_empty:
            task_metadata["workspace_base_snapshot"] = self._snapshot(durable_root)
            task["metadata"] = task_metadata
            task["updated_at"] = utcnow().isoformat()
            self.store.update_task(task)
        if any(task_root.iterdir()):
            return task_root, "existing"
        git_dir = durable_root / ".git"
        # A low-privilege account must not receive a ``.git`` file pointing
        # back into the durable source tree (which may be control-plane
        # private).  A directory copy also avoids leaking Git hooks/config.
        if git_dir.exists() and not isolated:
            completed = subprocess.run(
                ["git", "worktree", "add", "--detach", str(task_root), base_revision],
                cwd=str(durable_root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "could not create task Git worktree: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                )
            return task_root, "git_worktree"
        # A non-Git customer workspace still gets isolation. It cannot report
        # a Git diff, but the Task result retains the copied files until the
        # operator explicitly cleans the task directory.
        def _ignore_private_copy(directory: str, names: list[str]) -> set[str]:
            ignored = set(self._PRIVATE_COPY_NAMES)
            # Do not dereference customer-created symlinks while copying into
            # an agent root; a symlink can otherwise point at credentials or
            # the control-plane database outside the durable workspace.
            for name in names:
                try:
                    if (
                        (Path(directory) / name).is_symlink()
                        or self._private_relative(name)
                    ):
                        ignored.add(name)
                except OSError:
                    ignored.add(name)
            return ignored

        shutil.copytree(
            durable_root,
            task_root,
            dirs_exist_ok=True,
            ignore=_ignore_private_copy,
        )
        return task_root, "isolated_directory_copy" if isolated else "directory_copy"

    def publish_task_workspace(
        self, organization_id: str, task: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Publish isolated Task edits with optimistic conflict detection.

        A private worker never writes the durable workspace directly.  This
        operation compares the durable tree with the snapshot captured before
        the worker started, refuses the whole publish on concurrent edits, and
        moves published deletions into durable ``.trash`` for recovery.
        """
        durable = self.get_for_task(organization_id, task)
        if not durable or durable.get("status") != "active":
            raise ValueError("no active workspace resolved for task")
        if str(task.get("organization_id") or organization_id) != organization_id:
            raise ValueError("task does not belong to organization")
        durable_root = self.resolve_durable_path(durable)
        org_key = self._validate_component(organization_id, "organization id")
        task_key = self._validate_component(str(task.get("id") or ""), "task id")
        expected_task_root = self.agent_root / org_key / task_key
        if expected_task_root.is_symlink() or not expected_task_root.is_dir():
            raise ValueError("task workspace is missing and cannot be published")
        task_root = self.task_root(organization_id, task)
        if task_root == durable_root:
            raise ValueError("task workspace is not isolated")
        base = (task.get("metadata") or {}).get("workspace_base_snapshot")
        if not isinstance(base, dict):
            raise ValueError("task has no workspace baseline; it cannot be published safely")
        private_snapshot = self._snapshot(task_root)
        current_snapshot = self._snapshot(durable_root)
        changed = sorted(
            path
            for path in set(base) | set(private_snapshot)
            if private_snapshot.get(path) != base.get(path)
        )
        conflicts = [
            path for path in changed if current_snapshot.get(path) != base.get(path)
        ]
        if conflicts:
            return {
                "status": "conflict",
                "task_id": task["id"],
                "conflicts": conflicts[:500],
                "truncated": len(conflicts) > 500,
            }
        if not changed:
            return {"status": "no_changes", "task_id": task["id"], "files": []}

        quota = max(1_024, min(int(durable.get("quota_bytes") or 10_000_000), 1_000_000_000))
        current_bytes = sum(int(item.get("size") or 0) for item in current_snapshot.values())
        next_bytes = current_bytes
        for relative in changed:
            previous = current_snapshot.get(relative)
            replacement = private_snapshot.get(relative)
            next_bytes -= int((previous or {}).get("size") or 0)
            next_bytes += int((replacement or {}).get("size") or 0)
        if next_bytes > quota:
            raise ValueError(f"workspace quota exceeded ({quota} bytes)")
        if dry_run:
            return {
                "status": "preview",
                "task_id": task["id"],
                "files": [path for path in changed if path in private_snapshot][:500],
                "deletions": [path for path in changed if path not in private_snapshot][:500],
                "bytes_after": next_bytes,
                "truncated": len(changed) > 500,
            }

        # Validate every path and source before the first mutation.  This
        # keeps an unexpected symlink, special file, or disappearing source
        # from leaving a half-published result in the durable workspace.
        preflight: list[tuple[str, Path, Path, dict[str, Any] | None]] = []
        for relative in changed:
            destination = self._safe_snapshot_path(durable_root, relative)
            source = self._safe_snapshot_path(task_root, relative)
            replacement = private_snapshot.get(relative)
            if replacement is not None and (source.is_symlink() or not source.is_file()):
                raise ValueError(f"refusing to publish non-regular file: {relative}")
            if replacement is None and destination.exists() and destination.is_symlink():
                raise ValueError(f"refusing to delete symlink: {relative}")
            preflight.append((relative, destination, source, replacement))

        published: list[str] = []
        trashed: list[str] = []
        trash_base = durable_root / ".trash"
        if trash_base.is_symlink():
            raise ValueError("durable workspace trash cannot be a symlink")
        trash_base.mkdir(parents=True, exist_ok=True)
        if trash_base.resolve() != trash_base:
            raise ValueError("durable workspace trash escaped the durable root")
        trash_root = trash_base / str(task["id"])
        if trash_root.is_symlink():
            raise ValueError("durable workspace trash cannot be a symlink")
        for relative, destination, source, replacement in preflight:
            if replacement is None:
                if destination.exists() and not destination.is_symlink():
                    trash_root.mkdir(parents=True, exist_ok=True)
                    trash_destination = trash_root / Path(relative).name
                    counter = 1
                    while trash_destination.exists():
                        trash_destination = trash_root / f"{Path(relative).stem}-{counter}{Path(relative).suffix}"
                        counter += 1
                    shutil.move(str(destination), str(trash_destination))
                    trashed.append(str(trash_destination.relative_to(durable_root)))
                published.append(relative)
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"refusing to publish non-regular file: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(destination.parent), prefix=".toda-publish-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                try:
                    os.fchmod(temporary.fileno(), stat.S_IMODE(source.stat().st_mode))
                except OSError:
                    pass
            os.replace(temporary_path, destination)
            published.append(relative)

        updated_task = dict(task)
        updated_metadata = dict(updated_task.get("metadata") or {})
        updated_metadata["workspace_base_snapshot"] = self._snapshot(durable_root)
        updated_task["metadata"] = updated_metadata
        updated_task["updated_at"] = utcnow().isoformat()
        self.store.update_task(updated_task)
        self.audit.record(
            organization_id,
            "workspace.task_published",
            {"task_id": task["id"], "files": published, "trashed": trashed},
            principal_id=(task.get("envelope") or {}).get("principal_id"),
        )
        return {
            "status": "published",
            "task_id": task["id"],
            "files": published[:500],
            "trashed": trashed[:500],
            "truncated": len(published) > 500 or len(trashed) > 500,
        }

    @classmethod
    def _snapshot(cls, root: Path) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        if not root.is_dir() or root.is_symlink():
            return snapshot
        for item in sorted(root.rglob("*")):
            if item.is_symlink() or not item.is_file():
                continue
            relative = str(item.relative_to(root))
            if cls._private_relative(relative):
                continue
            try:
                digest = hashlib.sha256()
                with item.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                snapshot[relative] = {"sha256": digest.hexdigest(), "size": item.stat().st_size}
            except OSError:
                continue
        return snapshot

    @classmethod
    def _private_relative(cls, relative: str) -> bool:
        for part in Path(relative).parts:
            lower = part.lower()
            if lower in cls._PRIVATE_COPY_NAMES:
                return True
            if lower.startswith(".env") or "credential" in lower or "secret" in lower:
                return True
            if lower.endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db")):
                return True
        return False

    @classmethod
    def _safe_snapshot_path(cls, root: Path, relative: str) -> Path:
        if (
            not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or cls._private_relative(relative)
        ):
            raise ValueError("workspace snapshot contains an invalid path")
        raw = root / relative
        if raw == root or root not in raw.parents:
            raise ValueError("workspace snapshot path escaped its root")
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("workspace snapshot path traverses a symlink")
        candidate = raw.resolve()
        if candidate != raw or (candidate != root and root not in candidate.parents):
            raise ValueError("workspace snapshot path escaped its root")
        return candidate

    def cleanup_task_workspace(
        self, organization_id: str, task: dict[str, Any], *, remove: bool = False
    ) -> dict[str, Any]:
        """Optionally remove a task workspace, never its durable parent."""
        task_root = self.task_root(organization_id, task)
        if not remove:
            return {"path": str(task_root), "removed": False}
        if self.agent_root not in task_root.parents:
            raise ValueError("refusing to remove a path outside the agent root")
        durable = self.get_for_task(organization_id, task)
        durable_root = self.resolve_durable_path(durable) if durable else None
        if (task_root / ".git").is_file():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(task_root)],
                cwd=str(durable_root or self.root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        elif task_root.exists():
            shutil.rmtree(task_root)
        return {"path": str(task_root), "removed": True}

    def list_task_workspaces(self, organization_id: str) -> list[Path]:
        """Enumerate task roots beneath active organization workspaces."""
        if not organization_id or any(char in organization_id for char in "/\\"):
            return []
        roots: list[Path] = []
        org_root = (self.agent_root / organization_id).resolve()
        if self.agent_root not in org_root.parents or not org_root.is_dir():
            return roots
        for item in org_root.iterdir():
            # Never follow an attacker-controlled link during maintenance.
            # In particular, resolving a directory symlink before rmtree()
            # would turn a task cleanup into an arbitrary-path deletion.
            if item.is_symlink() or not item.is_dir():
                continue
            try:
                resolved = item.resolve(strict=True)
            except OSError:
                continue
            if resolved != item or resolved.parent != org_root:
                continue
            roots.append(resolved)
        return roots

    def cleanup_expired_tasks(
        self,
        organization_id: str,
        *,
        older_than_hours: int = 24,
        active_task_ids: set[str] | None = None,
    ) -> list[str]:
        """Remove only unreferenced task roots older than the retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, older_than_hours))
        active = active_task_ids or set()
        removed: list[str] = []
        org_root = (self.agent_root / organization_id).resolve()
        for path in self.list_task_workspaces(organization_id):
            if path.name in active:
                continue
            try:
                # Directory mtime only changes when entries are added/removed,
                # not when an existing source file is edited.  Prefer the
                # durable Task completion timestamp so a long-running task is
                # retained for the full configured window.
                task = self.store.get_task(path.name)
                reference = (task or {}).get("completed_at") or (task or {}).get("updated_at")
                modified = (
                    datetime.fromisoformat(str(reference))
                    if reference
                    else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                )
            except OSError:
                continue
            except (TypeError, ValueError):
                continue
            if modified >= cutoff:
                continue
            # Re-check the lexical and resolved boundary immediately before
            # deletion; a directory can be replaced between enumeration and
            # cleanup in a concurrent worker.
            if path.is_symlink() or path.parent != org_root or path.resolve() != path:
                continue
            shutil.rmtree(path)
            removed.append(str(path))
        # A stale detached worktree leaves an entry under the durable Git
        # admin directory even after its checkout directory is gone. Prune
        # only the known durable organization workspaces, never an arbitrary
        # repository supplied by a task.
        for record in self.list(organization_id):
            if record.get("status") != "active":
                continue
            try:
                durable = self.resolve_durable_path(record)
            except ValueError:
                continue
            if (durable / ".git").is_dir():
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(durable),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
        if removed:
            self.audit.record(
                organization_id,
                "workspace.task_roots.cleaned",
                {
                    "paths": removed,
                    "older_than_hours": max(1, older_than_hours),
                },
            )
        return removed

    def expire(self, workspace_id: str, organization_id: str) -> dict[str, Any] | None:
        """Manually archive a workspace folder's record as an org retention decision."""
        record = self.store.get_harness_record("workspaces", workspace_id)
        if not record or record.get("organization_id") != organization_id:
            return None
        record["status"] = "expired"
        record["updated_at"] = utcnow().isoformat()
        self.store.upsert_harness_projection("workspaces", record)
        self.audit.record(organization_id, "workspace.expired", {"workspace_id": workspace_id})
        return record
