"""Workspace-scoped file operations used by the harness tools.

This module deliberately has no knowledge of Toda's database or arbitrary host
paths.  A caller supplies a resolver for the active workspace root; every
operation then works with paths relative to that root and rejects traversal,
symlink escapes, and unbounded reads.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from toda.services.redaction import redact


PathResolver = Callable[[str, dict[str, Any] | None], Path]
RootResolver = Callable[[dict[str, Any] | None], Path]


class WorkspaceFilesystem:
    """Safe, bounded operations against one resolved workspace root."""

    _SENSITIVE_NAMES = frozenset(
        {
            ".env",
            ".env.local",
            ".env.production",
            ".aws",
            ".ssh",
            ".npmrc",
            ".pypirc",
            ".docker",
            ".git",
            ".toda-tasks",
            ".pi",
            ".trash",
            ".agent-home",
            "credentials",
            "secrets",
            "config.json",
            "brain.db",
            "brain.db-wal",
            "brain.db-shm",
            "id_rsa",
            "id_ed25519",
        }
    )

    def __init__(
        self,
        *,
        root: RootResolver,
        path: PathResolver,
        max_file_bytes: int = 1_000_000,
        max_workspace_bytes: int = 10_000_000,
    ) -> None:
        self._root = root
        self._path = path
        self.max_file_bytes = max_file_bytes
        self.max_workspace_bytes = max_workspace_bytes

    def list_files(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        root = self._root(runtime)
        base = self._path(str(args.get("path") or ""), runtime)
        if not base.exists():
            raise FileNotFoundError(str(args.get("path") or "."))
        pattern = str(args.get("glob") or "**/*")
        recursive = bool(args.get("recursive", True))
        include_hidden = bool(args.get("include_hidden", False))
        candidates = base.rglob("*") if recursive else base.glob("*")
        entries: list[dict[str, Any]] = []
        limit = min(int(args.get("limit", 200)), 500)
        for item in sorted(candidates):
            # ``Path.rglob`` yields symlink children and the normal Path
            # predicates follow them.  Never expose metadata for a link or a
            # child whose resolved target is outside the already-authorized
            # root; direct path resolution alone does not protect traversal.
            if not self._safe_item(item, root):
                continue
            relative = str(item.relative_to(root))
            if self._sensitive_relative(relative):
                continue
            if not include_hidden and any(part.startswith(".") for part in Path(relative).parts):
                continue
            if not fnmatch.fnmatch(relative, pattern) and not fnmatch.fnmatch(item.name, pattern):
                continue
            entries.append(
                {
                    "path": relative,
                    "type": "directory" if item.is_dir() else "file",
                    **({"bytes": item.stat().st_size} if item.is_file() else {}),
                }
            )
            if len(entries) >= limit:
                break
        return {"path": str(args.get("path") or ""), "entries": entries, "truncated": len(entries) >= limit}

    def read_file(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        target = self._path(str(args["path"]), runtime)
        if not target.is_file():
            raise FileNotFoundError(str(args["path"]))
        try:
            if target.stat().st_size > self.max_file_bytes:
                raise ValueError(f"file reads are limited to {self.max_file_bytes} bytes")
        except OSError as exc:
            raise FileNotFoundError(str(args["path"])) from exc
        max_chars = min(int(args.get("max_chars", 100_000)), 200_000)
        offset = max(int(args.get("offset", 1)), 1)
        limit = min(int(args.get("limit", 400)), 2_000)
        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        rendered = "\n".join(f"{offset + index}: {line}" for index, line in enumerate(selected))
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars]
        return {
            "path": str(args["path"]),
            "content": rendered,
            "start_line": offset,
            "end_line": min(offset + len(selected) - 1, len(lines)) if selected else offset - 1,
            "total_lines": len(lines),
            "truncated": len(selected) < max(0, len(lines) - offset + 1) or len(rendered) >= max_chars,
        }

    def search_files(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "")
        if not query:
            raise ValueError("query is required")
        if len(query) > 10_000:
            raise ValueError("query is too long")
        root = self._root(runtime)
        base = self._path(str(args.get("path") or ""), runtime)
        glob = str(args.get("glob") or "**/*")
        regex = bool(args.get("regex", False))
        flags = 0 if bool(args.get("case_sensitive", False)) else re.IGNORECASE
        matcher = re.compile(query, flags) if regex else None
        context_lines = min(max(int(args.get("context_lines", 0)), 0), 5)
        limit = min(int(args.get("limit", 100)), 500)
        matches: list[dict[str, Any]] = []
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for item in candidates:
            if not self._safe_item(item, root):
                continue
            if not item.is_file():
                continue
            relative = str(item.relative_to(root))
            if self._sensitive_relative(relative):
                continue
            try:
                if item.stat().st_size > self.max_file_bytes:
                    continue
            except OSError:
                continue
            if not fnmatch.fnmatch(relative, glob) and not fnmatch.fnmatch(item.name, glob):
                continue
            try:
                lines = item.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, 1):
                matched = bool(matcher.search(line)) if matcher else (
                    query in line if bool(args.get("case_sensitive", False)) else query.lower() in line.lower()
                )
                if not matched:
                    continue
                matches.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "text": line[:1_000],
                        **({
                            "context": lines[
                                max(0, line_number - 1 - context_lines) : line_number + context_lines
                            ]
                        } if context_lines else {}),
                    }
                )
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def write_file(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        target = self._path(str(args["path"]), runtime)
        content = str(args["content"])
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError(f"file writes are limited to {self.max_file_bytes} bytes")
        self._check_quota(target, len(encoded), runtime)
        self._reject_obvious_secret(content)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        return {"path": str(args["path"]), "created": not existed, "replaced": existed, "bytes": len(encoded)}

    def edit_file(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        target = self._path(str(args["path"]), runtime)
        try:
            if target.stat().st_size > self.max_file_bytes:
                raise ValueError(f"file edits are limited to {self.max_file_bytes} bytes")
        except OSError as exc:
            raise FileNotFoundError(str(args["path"])) from exc
        original = target.read_text(encoding="utf-8")
        old = str(args["old_text"])
        new = str(args["new_text"])
        if original.count(old) != 1:
            raise ValueError("old_text must match exactly once")
        updated = original.replace(old, new, 1)
        if len(updated.encode("utf-8")) > self.max_file_bytes:
            raise ValueError(f"file edits are limited to {self.max_file_bytes} bytes")
        self._reject_obvious_secret(updated)
        self._check_quota(target, len(updated.encode("utf-8")), runtime)
        target.write_text(updated, encoding="utf-8")
        return {"path": str(args["path"]), "changed": True, "bytes": len(updated.encode("utf-8"))}

    def move_file(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        source = self._path(str(args["source"]), runtime)
        destination = self._path(str(args["destination"]), runtime)
        if not source.is_file():
            raise FileNotFoundError(str(args["source"]))
        if destination.is_dir():
            raise IsADirectoryError(str(args["destination"]))
        if destination.exists() and not bool(args.get("overwrite", False)):
            raise FileExistsError(str(args["destination"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return {"source": str(args["source"]), "destination": str(args["destination"])}

    def delete_file(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        source = self._path(str(args["path"]), runtime)
        if not source.is_file():
            raise FileNotFoundError(str(args["path"]))
        root = self._root(runtime)
        if root.is_symlink():
            raise PermissionError("workspace root cannot be a symlink")
        task_id = str((runtime or {}).get("task_id") or "shared")
        if Path(task_id).name != task_id or task_id in {"", ".", ".."}:
            raise ValueError("invalid task id for workspace trash")
        trash_root = root / ".trash"
        if trash_root.is_symlink():
            raise PermissionError("workspace trash cannot be a symlink")
        trash_root.mkdir(parents=True, exist_ok=True)
        if trash_root.resolve() != trash_root:
            raise PermissionError("workspace trash escaped the workspace root")
        trash = trash_root / task_id
        if trash.is_symlink():
            raise PermissionError("workspace trash cannot be a symlink")
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / source.name
        counter = 1
        while target.exists():
            target = trash / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        shutil.move(str(source), str(target))
        return {"path": str(args["path"]), "trashed_as": str(target.relative_to(root))}

    def diff(self, args: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        before_path = self._path(str(args["before"]), runtime)
        after_path = self._path(str(args["after"]), runtime)
        for path, label in ((before_path, "before"), (after_path, "after")):
            try:
                if path.stat().st_size > self.max_file_bytes:
                    raise ValueError(f"{label} file exceeds the {self.max_file_bytes}-byte limit")
            except OSError as exc:
                raise FileNotFoundError(str(args[label])) from exc
        before = before_path.read_text(encoding="utf-8").splitlines()
        after = after_path.read_text(encoding="utf-8").splitlines()
        value = "\n".join(difflib.unified_diff(before, after, fromfile=str(args["before"]), tofile=str(args["after"]), lineterm=""))
        return {"diff": value[:100_000], "truncated": len(value) > 100_000}

    def _check_quota(self, target: Path, new_size: int, runtime: dict[str, Any]) -> None:
        root = self._root(runtime)
        quota = int((runtime or {}).get("workspace_quota_bytes") or self.max_workspace_bytes)
        quota = max(1_024, min(quota, 1_000_000_000))
        current = 0
        if root.exists():
            for item in root.rglob("*"):
                if not self._safe_item(item, root) or not item.is_file():
                    continue
                try:
                    current += item.stat().st_size
                except OSError:
                    continue
        try:
            previous = target.stat().st_size if target.exists() and not target.is_symlink() else 0
        except OSError:
            previous = 0
        if current - previous + new_size > quota:
            raise ValueError(f"workspace quota exceeded ({quota} bytes)")

    @classmethod
    def _sensitive_relative(cls, relative: str) -> bool:
        for part in Path(relative).parts:
            lower = part.lower()
            if lower in cls._SENSITIVE_NAMES:
                return True
            if lower.startswith(".env") or "credential" in lower or "secret" in lower:
                return True
            if lower.endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db")):
                return True
        return False

    @staticmethod
    def _safe_item(item: Path, root: Path) -> bool:
        if item.is_symlink():
            return False
        try:
            resolved = item.resolve(strict=False)
        except OSError:
            return False
        return resolved == root or root in resolved.parents

    @staticmethod
    def _reject_obvious_secret(content: str) -> None:
        if redact(content).redacted:
            raise ValueError("workspace files cannot contain obvious secrets")
