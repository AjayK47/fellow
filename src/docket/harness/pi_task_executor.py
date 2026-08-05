"""PiTaskExecutor: Phase 5's durable, non-blocking Pi Task runtime.

Runs Pi as a durable, detached worker_type=pi child Task created via
delegate_tasks. Per plan §10.1, Pi is a Task *runtime*, not a
synchronous tool handler:

- `pi` is spawned once, detached (its own process group), with output
  redirected to a log file inside the Task's persistent workspace and a
  trailing shell-appended exit-code marker -- not an in-process pipe held
  open by whichever worker happened to start it. That marker line is what
  makes "did it finish, and how" recoverable from nothing but the PID and
  the log path, by any worker, after any restart -- genuine durability, not
  just "don't block this one thread".
- `poll()` is the only other operation: cheap, idempotent, safe to call
  repeatedly from a self-rescheduling "task.pi_poll" job. Each call decides,
  from current evidence alone: finished (read the marker, finalize), still
  alive (renew the lease, maybe checkpoint-wake the Session, reschedule
  another poll), or gone-without-a-trace (crashed -- reclaim as failed).
- No total wall-clock timeout. The only two clocks that matter are
  heartbeat staleness (missing heartbeat => reclaim) and the checkpoint
  interval (still running => wake the Session Manager for a judgment call,
  not a verdict) -- see §10.3/§10.3.1.
"""

from __future__ import annotations

import os
import json
import re
import shlex
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from docket.domain.models import new_id, utcnow
from docket.harness.models import AgentRunState
from docket.harness.run_code import scrub_child_env
from docket.services.scope_resolution import resolve_scope

_MAX_OUTPUT_CHARS = 20_000
_EXIT_MARKER = "__PI_EXIT__"

CommandBuilder = Callable[[str], list[str]]


def default_pi_command(prompt: str) -> list[str]:
    """Build the detached Pi CLI command for one Task prompt."""
    return ["pi", "--prompt", prompt]


def check_hardline_floor(prompt: str, command: list[str], workspace_root: Path) -> str | None:
    """Reject the one invariant that cannot be overridden by model output.

    A Task explicitly saying not to push must not launch a command containing
    a git push, regardless of approval or later prompt interpretation.
    """
    lowered_prompt = prompt.lower()
    command_text = " ".join(command).lower()
    no_push = bool(re.search(r"\b(?:do not|don't|never|no)\s+push\b", lowered_prompt))
    if no_push and re.search(r"\bgit\s+push\b", command_text):
        return "Pi command blocked by Task hardline floor: pushing is forbidden"
    return None


def parse_terminal_event(output: str) -> dict[str, Any] | None:
    """Parse the final structured success event emitted by Pi, if present."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("status") in {"success", "completed"}:
            return event
    return None


def _is_alive(pid: int) -> bool:
    # These are always our own child processes (spawned via subprocess.Popen
    # in start()), so an exited-but-unreaped child is a zombie -- and a
    # zombie still answers kill(pid, 0) successfully. Reap it first
    # (WNOHANG, never blocks): a genuinely still-running process leaves it
    # untouched, but a finished one would otherwise look permanently
    # "alive" forever, since nothing else in this process ever calls wait()
    # on it.
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        # Not (or no longer) our child -- e.g. a worker restart lost the
        # parent relationship. Fall back to a plain existence check.
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just owned by someone else -- can't happen for our own
        # child, but treat as alive rather than silently declaring it dead.
        return True
    return True


def _wait_until_gone(pid: int, *, timeout_seconds: float = 1.0) -> bool:
    """Briefly wait for a just-killed child to exit and be reaped.

    SIGKILL is asynchronous.  A single immediate `_is_alive()` can observe
    the process before the kernel has completed termination, after which it
    becomes a zombie until another poll happens to reap it.  Cancellation is
    expected to be terminal in this poll, so perform a small bounded reap
    loop here instead of leaving that race to the next scheduled job.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.01)
    return not _is_alive(pid)


class PiTaskExecutor:
    def __init__(
        self,
        *,
        store: Any,
        sessions: Any,
        workspaces: Any,
        artifacts: Any,
        audit: Any,
        command_builder: CommandBuilder = default_pi_command,
        checkpoint_interval_seconds: int = 3600,
        stale_heartbeat_seconds: int = 1800,
        poll_interval_seconds: int = 10,
        cancel_grace_seconds: int = 20,
        lease_seconds: int = 1800,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.audit = audit
        self.command_builder = command_builder
        self.checkpoint_interval_seconds = checkpoint_interval_seconds
        self.stale_heartbeat_seconds = stale_heartbeat_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.cancel_grace_seconds = cancel_grace_seconds
        self.lease_seconds = lease_seconds

    # ---- start ----

    def start(self, task_id: str, *, owner: str, enqueue_poll: Any) -> dict[str, Any]:
        """Claim and spawn a Pi Task's first attempt. `enqueue_poll(task_id,
        available_at)` schedules the next poll -- injected so this class
        never has to know about the job queue's own signature."""
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"task not found: {task_id}")
        if not self.store.claim_task(task_id, owner=owner, lease_seconds=self.lease_seconds):
            current = self.store.get_task(task_id) or task
            return {"task_id": task_id, "started": False, "state": current["state"], "reason": "already claimed"}
        organization_id = task["organization_id"]
        workspace = self.workspaces.get_for_task(organization_id, task) if self.workspaces else None
        if not workspace:
            self._fail(task, owner=owner, error="no persistent workspace resolved for this task")
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": "no workspace"}
        workspace_root = Path(workspace["path"]).resolve()
        prompt = task.get("prompt") or task.get("goal") or ""
        command = self.command_builder(prompt)

        violation = check_hardline_floor(prompt, command, workspace_root)
        if violation:
            self._record_blocked_attempt(task, command=command, workspace_root=workspace_root, violation=violation)
            self._fail(task, owner=owner, error=violation)
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": violation}

        pi_dir = workspace_root / ".pi"
        pi_dir.mkdir(parents=True, exist_ok=True)
        attempt_number = len(self.store.list_task_attempts(task_id)) + 1
        attempt_id = new_id("attempt")
        log_path = pi_dir / f"{task_id}-{attempt_number}.log"

        shell_command = (
            f"{shlex.join(command)} > {shlex.quote(str(log_path))} 2>&1; "
            f'printf "\\n{_EXIT_MARKER}:%s\\n" "$?" >> {shlex.quote(str(log_path))}'
        )
        proc = subprocess.Popen(
            ["/bin/sh", "-c", shell_command],
            cwd=str(workspace_root),
            env=scrub_child_env(dict(os.environ)),
            start_new_session=True,
        )
        now = utcnow().isoformat()
        attempt = {
            "id": attempt_id,
            "organization_id": organization_id,
            "task_id": task_id,
            "attempt_number": attempt_number,
            "status": "running",
            "owner": owner,
            "pid": proc.pid,
            "command": command,
            "workspace_path": str(workspace_root),
            "log_path": str(log_path),
            "started_at": now,
            "last_heartbeat_at": now,
            "last_checkpoint_at": now,
            "cancel_requested_at": None,
            "cancellation_reason": None,
            "exit_code": None,
            "terminal_event": None,
            "output_tail": "",
            "created_at": now,
            "updated_at": now,
        }
        self.store.create_task_attempt(attempt)
        task["state"] = AgentRunState.RUNNING.value
        task["active_attempt_id"] = attempt_id
        task["updated_at"] = now
        if not self.store.update_task_owned(task, owner=owner):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise RuntimeError(f"pi task lease lost while starting {task_id}")
        self.audit.record(
            organization_id,
            "task.pi.started",
            {"task_id": task_id, "attempt_id": attempt_id, "pid": proc.pid},
            principal_id=task["envelope"]["principal_id"],
            trace_id=task.get("trace_id"),
        )
        enqueue_poll(task_id, available_at=None)
        return {
            "task_id": task_id,
            "started": True,
            "state": AgentRunState.RUNNING.value,
            "attempt_id": attempt_id,
            "pid": proc.pid,
        }

    # ---- poll ----

    def poll(self, task_id: str, *, enqueue_poll: Any) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"task not found: {task_id}")
        if task["state"] in {
            AgentRunState.COMPLETED.value,
            AgentRunState.FAILED.value,
            AgentRunState.CANCELLED.value,
        }:
            return {"task_id": task_id, "state": task["state"]}
        attempt = self._active_attempt(task)
        if not attempt:
            return {"task_id": task_id, "state": task["state"], "note": "no active attempt"}
        owner = attempt["owner"]
        now_dt = utcnow()
        now = now_dt.isoformat()

        marker = self._read_exit_marker(attempt["log_path"])
        if marker is not None:
            exit_code, output = marker
            result = self._finalize(
                task, attempt, owner=owner, exit_code=exit_code, output=output,
                enqueue_poll=enqueue_poll,
            )
            return {"task_id": task_id, "state": result["state"], "finished": True}

        alive = _is_alive(attempt["pid"])
        stale = (
            now_dt - _parse(attempt["last_heartbeat_at"])
        ).total_seconds() > self.stale_heartbeat_seconds

        if alive and attempt.get("cancel_requested_at"):
            # Plain SIGTERM to the process group doesn't reliably reach a
            # /bin/sh -c wrapper blocked synchronously on its foreground
            # child (observed: bash defers signal delivery until the
            # child exits, on this platform's /bin/sh) -- escalate to
            # SIGKILL once the grace period elapses rather than waiting
            # indefinitely for a graceful exit that may never come.
            elapsed = (now_dt - _parse(attempt["cancel_requested_at"])).total_seconds()
            if elapsed >= self.cancel_grace_seconds:
                try:
                    os.killpg(os.getpgid(attempt["pid"]), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                alive = not _wait_until_gone(attempt["pid"])

        if not alive or stale:
            reason = "process not found (crashed or killed without a recorded exit)" if not alive else "missing heartbeat exceeded stale threshold"
            result = self._finalize(
                task, attempt, owner=owner, exit_code=None, output=self._tail_log(attempt["log_path"]),
                forced_reason=reason, enqueue_poll=enqueue_poll,
            )
            return {"task_id": task_id, "state": result["state"], "finished": True, "reason": reason}

        # Still genuinely running: renew the lease, heartbeat, maybe
        # checkpoint-wake, and reschedule the next poll.
        if not self.store.renew_task_lease(task_id, owner=owner, lease_seconds=self.lease_seconds):
            # Lost the lease to a reclaim between the state check above and
            # here -- back off, whoever reclaimed it owns the next step.
            return {"task_id": task_id, "state": task["state"], "note": "lease lost"}
        attempt["last_heartbeat_at"] = now
        checkpointed = False
        if (
            now_dt - _parse(attempt["last_checkpoint_at"])
        ).total_seconds() >= self.checkpoint_interval_seconds:
            attempt["last_checkpoint_at"] = now
            checkpointed = True
        attempt["updated_at"] = now
        self.store.update_task_attempt(attempt)
        if checkpointed:
            self.audit.record(
                task["organization_id"],
                "task.pi.checkpoint",
                {
                    "task_id": task_id,
                    "attempt_id": attempt["id"],
                    "output_tail": self._tail_log(attempt["log_path"])[-2000:],
                },
                principal_id=task["envelope"]["principal_id"],
                trace_id=task.get("trace_id"),
            )
            if task.get("session_id"):
                # Liveness is not progress (§10.3.1) -- the Manager decides
                # whether "still going" is fine, not this executor.
                self.store.bump_task_revision(task["id"])
                if task.get("parent_task_id"):
                    self.store.bump_task_revision(task["parent_task_id"])
                self.sessions.notify(task["session_id"], reason="task.pi.checkpoint")
        enqueue_poll(task_id, available_at=_future(now_dt, self.poll_interval_seconds))
        return {"task_id": task_id, "state": task["state"], "alive": True, "checkpointed": checkpointed}

    # ---- cancel ----

    def cancel(self, task_id: str, *, reason: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"task not found: {task_id}")
        attempt = self._active_attempt(task)
        if not attempt:
            return {"task_id": task_id, "cancelled": False, "reason": "no active attempt"}
        pid = attempt["pid"]
        now = utcnow().isoformat()
        if _is_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        attempt["cancel_requested_at"] = now
        attempt["cancellation_reason"] = reason
        attempt["updated_at"] = now
        self.store.update_task_attempt(attempt)
        self.audit.record(
            task["organization_id"],
            "task.pi.cancel_requested",
            {"task_id": task_id, "attempt_id": attempt["id"], "reason": reason},
            principal_id=task["envelope"]["principal_id"],
            trace_id=task.get("trace_id"),
        )
        return {"task_id": task_id, "cancelled": True}

    # ---- internals ----

    def _active_attempt(self, task: dict[str, Any]) -> dict[str, Any] | None:
        attempt_id = task.get("active_attempt_id")
        if not attempt_id:
            return None
        for attempt in self.store.list_task_attempts(task["id"]):
            if attempt["id"] == attempt_id:
                return attempt
        return None

    @staticmethod
    def _read_exit_marker(log_path: str) -> tuple[int, str] | None:
        path = Path(log_path)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        marker_prefix = f"{_EXIT_MARKER}:"
        idx = text.rfind(marker_prefix)
        if idx == -1:
            return None
        tail = text[idx + len(marker_prefix):].strip().splitlines()
        if not tail:
            return None
        try:
            exit_code = int(tail[0].strip())
        except ValueError:
            return None
        output = text[:idx]
        return exit_code, output

    @staticmethod
    def _tail_log(log_path: str) -> str:
        path = Path(log_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:]

    def _finalize(
        self,
        task: dict[str, Any],
        attempt: dict[str, Any],
        *,
        owner: str,
        exit_code: int | None,
        output: str,
        forced_reason: str | None = None,
        enqueue_poll: Any = None,
    ) -> dict[str, Any]:
        # A completion marker can be observed by a delayed duplicate poll.
        # Revalidate the live lease before changing the attempt, creating an
        # artifact, auditing completion, or waking the parent Session.
        if not self.store.renew_task_lease(
            task["id"], owner=owner, lease_seconds=self.lease_seconds
        ):
            raise RuntimeError(f"pi task lease lost while finalizing {task['id']}")
        terminal_event = parse_terminal_event(output) if output else None
        # A cancelled attempt reaches here via the same "process no longer
        # alive, no exit marker" path as a genuine crash (SIGTERM usually
        # kills the whole /bin/sh -c chain before it reaches the trailing
        # marker printf) -- cancellation must be checked first, or every
        # deliberate cancel would be misreported as a crash.
        cancelled = bool(attempt.get("cancel_requested_at"))
        if cancelled:
            status = "cancelled"
        elif forced_reason:
            status = "crashed"
        elif terminal_event is not None and terminal_event.get("status") == "success":
            status = "succeeded"
        else:
            status = "failed"

        # manage_child_task(action="revise") on a pi child cancels the
        # current attempt and marks this on the Task rather than trying to
        # race a live redirect (plan §10.5: pi has no documented interactive
        # protocol) -- once that cancellation is confirmed here, immediately
        # start a fresh attempt with the already-revised prompt from the
        # same workspace, instead of finalizing as a terminal CANCELLED.
        pending_revision = (task.get("metadata") or {}).get("pending_revision")
        if status == "cancelled" and pending_revision:
            now = utcnow().isoformat()
            attempt.update(
                {
                    "status": "cancelled",
                    "exit_code": exit_code,
                    "output_tail": output[-_MAX_OUTPUT_CHARS:],
                    "terminal_event": terminal_event,
                    "forced_reason": "revised -- restarting with new prompt",
                    "updated_at": now,
                }
            )
            self.store.update_task_attempt(attempt)
            task_metadata = dict(task.get("metadata") or {})
            task_metadata.pop("pending_revision", None)
            task["metadata"] = task_metadata
            task.pop("active_attempt_id", None)
            task["updated_at"] = now
            if not self.store.update_task_owned(task, owner=owner):
                raise RuntimeError(
                    f"pi task lease lost while restarting {task['id']}"
                )
            # Non-terminal release so claim_task can immediately re-claim it.
            if not self.store.release_task(
                task["id"], owner=owner, next_state=AgentRunState.QUEUED.value
            ):
                raise RuntimeError(
                    f"pi task lease lost while restarting {task['id']}"
                )
            self.audit.record(
                task["organization_id"],
                "task.pi.revision_restart",
                {"task_id": task["id"], "attempt_id": attempt["id"]},
                principal_id=task["envelope"]["principal_id"],
                trace_id=task.get("trace_id"),
            )
            return self.start(task["id"], owner=owner, enqueue_poll=enqueue_poll)

        now = utcnow().isoformat()
        attempt.update(
            {
                "status": status,
                "exit_code": exit_code,
                "output_tail": output[-_MAX_OUTPUT_CHARS:],
                "terminal_event": terminal_event,
                "forced_reason": forced_reason,
                "updated_at": now,
            }
        )
        self.store.update_task_attempt(attempt)

        principal_id = task["envelope"]["principal_id"]
        answer = (
            (terminal_event or {}).get("summary")
            or (terminal_event or {}).get("message")
            or output[-4000:]
            or (forced_reason or "")
        )
        artifact = None
        if self.artifacts:
            scope_id = resolve_scope(
                store=self.store,
                organization_id=task["organization_id"],
                principal_id=principal_id,
                destination=task["envelope"]["destination"],
            ).write_scope_id
            artifact = self.artifacts.create(
                organization_id=task["organization_id"],
                artifact_type="pi_attempt_result",
                title=f"Pi attempt: {(task.get('prompt') or '')[:100]}",
                content={
                    "status": status,
                    "exit_code": exit_code,
                    "terminal_event": terminal_event,
                    "output": output[-_MAX_OUTPUT_CHARS:],
                },
                scope_id=scope_id,
                task_id=task["id"],
                created_by=principal_id,
                metadata={"attempt_id": attempt["id"], "task_id": task["id"]},
            )
        task.pop("active_attempt_id", None)
        if status == "succeeded":
            next_state = AgentRunState.COMPLETED.value
            task["final_answer"] = answer
            task["result"] = answer
            task["updated_at"] = now
            released = self.store.release_task(
                task["id"], owner=owner, next_state=next_state, task=task
            )
        else:
            next_state = (
                AgentRunState.CANCELLED.value if status == "cancelled" else AgentRunState.FAILED.value
            )
            task["error"] = forced_reason or answer or f"pi attempt {status}"
            task["updated_at"] = now
            released = self.store.release_task(
                task["id"], owner=owner, next_state=next_state, task=task
            )
        if not released:
            raise RuntimeError(f"pi task lease lost while finalizing {task['id']}")
        self.audit.record(
            task["organization_id"],
            "task.pi.completed",
            {
                "task_id": task["id"],
                "attempt_id": attempt["id"],
                "status": status,
                "artifact_id": (artifact or {}).get("id"),
            },
            principal_id=principal_id,
            trace_id=task.get("trace_id"),
        )
        parent = (
            self.store.get_task(task["parent_task_id"])
            if task.get("parent_task_id")
            else None
        )
        if (
            task.get("session_id")
            and parent
            and parent["state"] not in {"completed", "failed", "cancelled"}
        ):
            self.store.bump_task_revision(task["id"])
            self.store.bump_task_revision(parent["id"])
            self.sessions.notify(task["session_id"], reason=f"task.pi.{status}")
        return {"task_id": task["id"], "state": next_state, "status": status}

    def _fail(self, task: dict[str, Any], *, owner: str, error: str) -> None:
        now = utcnow().isoformat()
        task["error"] = error
        task["updated_at"] = now
        if not self.store.release_task(
            task["id"],
            owner=owner,
            next_state=AgentRunState.FAILED.value,
            task=task,
        ):
            raise RuntimeError(f"pi task lease lost while failing {task['id']}")
        parent = (
            self.store.get_task(task["parent_task_id"])
            if task.get("parent_task_id")
            else None
        )
        if (
            task.get("session_id")
            and parent
            and parent["state"] not in {"completed", "failed", "cancelled"}
        ):
            self.store.bump_task_revision(task["id"])
            self.store.bump_task_revision(parent["id"])
            self.sessions.notify(task["session_id"], reason="task.pi.failed")

    def _record_blocked_attempt(
        self, task: dict[str, Any], *, command: list[str], workspace_root: Path, violation: str
    ) -> None:
        now = utcnow().isoformat()
        attempt = {
            "id": new_id("attempt"),
            "organization_id": task["organization_id"],
            "task_id": task["id"],
            "attempt_number": len(self.store.list_task_attempts(task["id"])) + 1,
            "status": "blocked",
            "owner": None,
            "pid": None,
            "command": command,
            "workspace_path": str(workspace_root),
            "log_path": None,
            "started_at": now,
            "last_heartbeat_at": now,
            "last_checkpoint_at": now,
            "cancel_requested_at": None,
            "cancellation_reason": None,
            "exit_code": None,
            "terminal_event": None,
            "output_tail": "",
            "forced_reason": violation,
            "created_at": now,
            "updated_at": now,
        }
        self.store.create_task_attempt(attempt)
        self.audit.record(
            task["organization_id"],
            "task.pi.blocked",
            {"task_id": task["id"], "reason": violation},
            principal_id=task["envelope"]["principal_id"],
            trace_id=task.get("trace_id"),
        )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _future(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()
