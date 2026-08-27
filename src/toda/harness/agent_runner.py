"""Low-privilege local process runner for coding workers.

The runner is intentionally narrow: it starts a configured executable from a
task worktree with a scrubbed environment and, when configured, drops to a
dedicated Unix account before the child executes.  It is not a general shell
service and never accepts a caller-provided host path outside the already
resolved worktree.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from toda.harness.run_code import scrub_child_env


class AgentRunner:
    def __init__(
        self,
        *,
        execution_user: str | None = None,
        execution_home: Path | None = None,
        cpu_seconds: int | None = None,
        max_processes: int | None = None,
        max_file_bytes: int | None = None,
        memory_bytes: int | None = None,
    ) -> None:
        self.execution_user = execution_user
        self.execution_home = execution_home
        self.cpu_seconds = cpu_seconds
        self.max_processes = max_processes
        self.max_file_bytes = max_file_bytes
        self.memory_bytes = memory_bytes

    def environment(
        self,
        source: dict[str, str],
        *,
        credential_env: str | None = None,
        credential: str | None = None,
        working_directory: Path | None = None,
    ) -> dict[str, str]:
        env = scrub_child_env(source)
        if self.execution_home:
            env["HOME"] = str(self.execution_home)
        elif working_directory is not None:
            # Never leak the control-plane user's home into a coding worker.
            # Pi may still create local config/cache files, but they live in
            # the disposable task directory instead.
            local_home = working_directory / ".agent-home"
            local_home.mkdir(parents=True, exist_ok=True)
            try:
                local_home.chmod(0o700)
            except OSError:
                pass
            env["HOME"] = str(local_home)
        if credential_env and credential:
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", credential_env)
                or credential_env.startswith(("PATH", "HOME", "TMP", "TEMP", "SHELL"))
            ):
                raise ValueError("credential_env must be a dedicated non-process environment variable")
            env[credential_env] = credential
        return env

    def start(
        self,
        *,
        shell_command: str,
        cwd: Path,
        env: dict[str, str],
        stdin: Any = subprocess.DEVNULL,
        isolate_parent: bool = False,
        **popen_options: Any,
    ) -> subprocess.Popen[Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": stdin,
            "start_new_session": True,
            # Agent-created files (including Pi logs and caches) must never be
            # world-readable, even when the service's umask is permissive.
            "umask": 0o077,
            **popen_options,
        }
        if self.execution_user:
            if os.geteuid() != 0:
                raise PermissionError(
                    f"execution user {self.execution_user!r} requires a privileged runner"
                )
            import pwd

            try:
                account = pwd.getpwnam(self.execution_user)
            except KeyError as exc:
                raise PermissionError(
                    f"execution user {self.execution_user!r} does not exist"
                ) from exc
            if self.execution_home is not None:
                self._prepare_execution_home(self.execution_home, account.pw_uid, account.pw_gid)
            self._chown_tree(
                cwd,
                account.pw_uid,
                account.pw_gid,
                include_parent=isolate_parent,
            )
            # Use Popen's native credential drop instead of a setuid
            # preexec_fn.  Native user/group handling avoids the documented
            # fork-in-a-multithreaded-process deadlock hazard; preexec_fn is
            # reserved for resource limits only.
            kwargs["user"] = account.pw_uid
            kwargs["group"] = account.pw_gid
            kwargs["extra_groups"] = []
            if any(
                value is not None
                for value in (self.cpu_seconds, self.max_processes, self.max_file_bytes, self.memory_bytes)
            ):
                kwargs["preexec_fn"] = self._limits_preexec
        elif any(
            value is not None
            for value in (self.cpu_seconds, self.max_processes, self.max_file_bytes, self.memory_bytes)
        ):
            kwargs["preexec_fn"] = self._limits_preexec
        return subprocess.Popen(["/bin/sh", "-c", shell_command], **kwargs)

    @staticmethod
    def _prepare_execution_home(home: Path, uid: int, gid: int) -> None:
        raw_home = home.expanduser()
        if not raw_home.is_absolute():
            raise RuntimeError("execution home must be an absolute dedicated directory")
        if raw_home == Path(raw_home.anchor or "/") or raw_home == Path.home():
            raise RuntimeError("execution home must be a dedicated child directory")
        if raw_home.is_symlink():
            raise RuntimeError("execution home cannot be a symlink")
        current = Path(raw_home.anchor or "/")
        for part in raw_home.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise RuntimeError("execution home cannot traverse symlinks")
        home = raw_home.resolve()
        home.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(home, uid, gid, follow_symlinks=False)
            os.chmod(home, 0o700)
        except OSError as exc:
            raise RuntimeError(f"could not secure execution home: {exc}") from exc

    def _set_limits(self) -> None:
        try:
            import resource
        except ImportError:
            return
        if self.cpu_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_seconds, self.cpu_seconds))
        if self.max_processes is not None:
            resource.setrlimit(resource.RLIMIT_NPROC, (self.max_processes, self.max_processes))
        if self.max_file_bytes is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (self.max_file_bytes, self.max_file_bytes))
        if self.memory_bytes is not None and hasattr(resource, "RLIMIT_AS"):
            resource.setrlimit(resource.RLIMIT_AS, (self.memory_bytes, self.memory_bytes))

    def _limits_preexec(self) -> None:
        self._set_limits()

    @staticmethod
    def _chown_tree(root: Path, uid: int, gid: int, *, include_parent: bool = False) -> None:
        boundaries = [root]
        if include_parent:
            # Pi task roots are <agent-root>/<org>/<task>. Make the org
            # boundary agent-owned and private, while never touching shared
            # directories such as /tmp used by run_code.
            boundaries.insert(0, root.parent)
        for boundary in boundaries:
            try:
                os.chown(boundary, uid, gid, follow_symlinks=False)
                os.chmod(boundary, 0o700)
            except OSError as exc:
                raise RuntimeError(f"could not assign task workspace ownership: {exc}") from exc
        for item in (root, *root.rglob("*")):
            try:
                os.chown(item, uid, gid, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not assign task worktree ownership: {exc}") from exc
