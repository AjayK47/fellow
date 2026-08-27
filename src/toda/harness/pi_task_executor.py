"""PiTaskExecutor: durable, non-blocking Pi Task runtime.

Runs Pi as a durable, detached worker_type=pi child Task created via
task_delegate. Per plan §10.1, Pi is a Task *runtime*, not a
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

from toda.domain.models import new_id, utcnow
from toda.config import settings
from toda.harness.hazard import hazard_reason
from toda.harness.models import AgentRunState
from toda.harness.agent_runner import AgentRunner
from toda.services.scope_resolution import resolve_scope
from toda.services.redaction import redact

_MAX_OUTPUT_CHARS = 20_000
_EXIT_MARKER = "__PI_EXIT__"

CommandBuilder = Callable[[str], list[str]]


def pi_command_builder(*, provider: str, model: str) -> CommandBuilder:
    """Build the detached Pi CLI command for one Task prompt.

    `--print` is Pi's non-interactive mode and the prompt is positional --
    there is no `--prompt` flag. `--mode json` makes the run machine-readable
    (see parse_terminal_event); the provider and model are passed explicitly
    so a Task never inherits whichever model the operator last selected in
    Pi's own TUI. The credential is deliberately *not* passed as `--api-key`:
    it goes through the child's environment, where `ps` cannot read it.
    """

    def build(prompt: str) -> list[str]:
        return [
            "pi",
            "--print",
            "--mode",
            "json",
            "--provider",
            provider,
            "--model",
            model,
            prompt,
        ]

    return build


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


def prescan_pi_launch(prompt: str, command: list[str]) -> str | None:
    """A deterministic observation about this launch, for the reviewer.

    Not a block. Pi is an agent-account coding subprocess and the whole point
    is that it gets on with the work; refusing outright on a pattern match
    would make it useless for exactly the debugging tasks it exists for. This
    only gives the reviewer something concrete to weigh.

    Uses Toda's data dir, NOT the workspace: the workspace is precisely
    where Pi is supposed to be writing, so scanning against it would flag
    every legitimate run.
    """
    return hazard_reason(
        f"{prompt}\n{' '.join(command)}", data_dir=settings.storage.data_dir
    )


def parse_terminal_event(output: str) -> dict[str, Any] | None:
    """Summarize Pi's closing `agent_end` event from its JSON output stream.

    Pi writes one JSON object per line and ends with `agent_end` -- carrying
    the whole message list and its usage -- followed by a bare
    `agent_settled`. Neither reports success: that is the exit marker's job.
    So this extracts the part a Session Manager actually needs handed back,
    the final assistant text and what the run cost, rather than leaving the
    caller to slice it out of a stream that repeats the full message object
    on every token delta.
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "agent_end":
            continue
        messages = event.get("messages") or []
        final = messages[-1] if isinstance(messages, list) and messages else {}
        blocks = final.get("content") or []
        text = "\n".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        return {
            "type": "agent_end",
            "text": text,
            "model": final.get("model"),
            "stop_reason": final.get("stopReason"),
            "usage": final.get("usage") or {},
            "will_retry": bool(event.get("willRetry")),
        }
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
        action_reviewer: Any | None = None,
        provider: str,
        model: str,
        credential_env: str | None = None,
        credential: str | None = None,
        execution_user: str | None = None,
        isolation_required: bool = True,
        execution_home: Path | None = None,
        cpu_seconds: int | None = None,
        max_processes: int | None = None,
        max_file_bytes: int | None = None,
        memory_bytes: int | None = None,
        command_builder: CommandBuilder | None = None,
        checkpoint_interval_seconds: int = 3600,
        stale_heartbeat_seconds: int = 1800,
        poll_interval_seconds: int = 10,
        cancel_grace_seconds: int = 20,
        lease_seconds: int = 1800,
    ) -> None:
        self.store = store
        # Pi is the execution path with network access. It runs under the
        # configured low-privilege agent account and a scrubbed environment;
        # routing launch through the same reviewer as other risky actions
        # still gives it a judgment step without a human pause for routine
        # coding work.
        self.action_reviewer = action_reviewer
        self.sessions = sessions
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.audit = audit
        self.credential_env = credential_env
        self.credential = credential
        self.execution_user = execution_user
        self.isolation_required = bool(isolation_required)
        self.execution_home = execution_home
        self.runner = AgentRunner(
            execution_user=execution_user,
            execution_home=execution_home,
            cpu_seconds=cpu_seconds,
            max_processes=max_processes,
            max_file_bytes=max_file_bytes,
            memory_bytes=memory_bytes,
        )
        self.command_builder = command_builder or pi_command_builder(
            provider=provider, model=model
        )
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
        if self.isolation_required and not self.execution_user:
            self._fail(
                task,
                owner=owner,
                error=(
                    "Pi coding is disabled for this deployment: configure a "
                    "dedicated pi_execution_user and agent workspace root "
                    "before launching an agent process."
                ),
            )
            return {
                "task_id": task_id,
                "started": False,
                "state": AgentRunState.FAILED.value,
                "reason": "dedicated execution user required",
            }
        organization_id = task["organization_id"]
        workspace = self.workspaces.get_for_task(organization_id, task) if self.workspaces else None
        if not workspace:
            self._fail(task, owner=owner, error="no persistent workspace resolved for this task")
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": "no workspace"}
        try:
            durable_workspace_root = self.workspaces.resolve_durable_path(workspace)
        except (AttributeError, ValueError) as exc:
            self._fail(task, owner=owner, error=f"invalid durable workspace: {exc}")
            return {
                "task_id": task_id,
                "started": False,
                "state": AgentRunState.FAILED.value,
                "reason": "invalid durable workspace",
            }
        base_revision = str((task.get("metadata") or {}).get("base_revision") or "HEAD")
        try:
            workspace_root, workspace_mode = self.workspaces.prepare_task_workspace(
                organization_id,
                task,
                base_revision=base_revision,
                isolated=bool(self.execution_user),
            )
        except Exception as exc:
            self._fail(task, owner=owner, error=f"could not prepare task workspace: {exc}")
            return {
                "task_id": task_id,
                "started": False,
                "state": AgentRunState.FAILED.value,
                "reason": "workspace preparation failed",
            }
        prompt = task.get("prompt") or task.get("goal") or ""
        command = self.command_builder(prompt)

        violation = check_hardline_floor(prompt, command, workspace_root)
        if violation:
            self._record_blocked_attempt(task, command=command, workspace_root=workspace_root, violation=violation)
            self._fail(task, owner=owner, error=violation)
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": violation}

        flagged = prescan_pi_launch(prompt, command)
        if flagged and self.action_reviewer is not None:
            verdict = self.action_reviewer.review(
                tool_name="pi_coding_worker",
                description=(
                    "Launch a Pi coding subprocess with network access in an "
                    "isolated, agent-owned Task workspace."
                ),
                arguments={
                    "prompt": redact(prompt).text,
                    "command": [redact(str(item)).text for item in command],
                },
                hazard=flagged,
            )
            if verdict == "deny":
                denial = f"Pi launch denied by review: it {flagged}"
                self._record_blocked_attempt(
                    task, command=command, workspace_root=workspace_root,
                    violation=denial,
                )
                self._fail(task, owner=owner, error=denial)
                return {"task_id": task_id, "started": False,
                        "state": AgentRunState.FAILED.value, "reason": denial}

        pi_dir = workspace_root / ".pi"
        if pi_dir.is_symlink():
            self._fail(task, owner=owner, error="Pi metadata directory cannot be a symlink")
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": "unsafe Pi metadata directory"}
        pi_dir.mkdir(parents=True, exist_ok=True)
        if pi_dir.resolve() != pi_dir:
            self._fail(task, owner=owner, error="Pi metadata directory escaped its workspace")
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": "unsafe Pi metadata directory"}
        attempt_number = len(self.store.list_task_attempts(task_id)) + 1
        attempt_id = new_id("attempt")
        log_path = pi_dir / f"{task_id}-{attempt_number}.log"
        if log_path.is_symlink():
            self._fail(task, owner=owner, error="Pi log path cannot be a symlink")
            return {"task_id": task_id, "started": False, "state": AgentRunState.FAILED.value, "reason": "unsafe Pi log path"}

        shell_command = (
            f"{shlex.join(command)} > {shlex.quote(str(log_path))} 2>&1; "
            f'printf "\\n{_EXIT_MARKER}:%s\\n" "$?" >> {shlex.quote(str(log_path))}'
        )
        # scrub_child_env drops every name containing KEY/TOKEN/AUTH, which
        # is right for run_code but leaves Pi with no way to reach a model.
        # Re-add exactly the one credential it needs, by name, rather than
        # widening the scrub for everything.
        child_env = self.runner.environment(
            dict(os.environ),
            credential_env=self.credential_env,
            credential=self.credential,
            working_directory=workspace_root,
        )
        try:
            proc = self.runner.start(
                shell_command=shell_command,
                cwd=workspace_root,
                env=child_env,
                isolate_parent=True,
            )
        except (OSError, PermissionError, RuntimeError, subprocess.SubprocessError) as exc:
            self._fail(task, owner=owner, error=f"could not start Pi runner: {exc}")
            return {
                "task_id": task_id,
                "started": False,
                "state": AgentRunState.FAILED.value,
                "reason": "runner unavailable",
            }
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
            "durable_workspace_path": str(durable_workspace_root),
            "workspace_mode": workspace_mode,
            "base_revision": base_revision,
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
        task_metadata = dict(task.get("metadata") or {})
        task_metadata.update(
            {
                "pi_workspace_path": str(workspace_root),
                "pi_workspace_mode": workspace_mode,
                "pi_base_revision": base_revision,
            }
        )
        task["metadata"] = task_metadata
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
        )
        return {"task_id": task_id, "cancelled": True}

    # ---- internals ----

    def _redact_text(self, value: str) -> str:
        """Redact known credential formats and this Pi provider key itself."""
        result = redact(value).text
        if self.credential and len(self.credential) >= 8:
            result = result.replace(self.credential, "[REDACTED:provider_credential]")
        return result

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
        if not path.exists() or path.is_symlink() or not path.is_file():
            return None
        text = PiTaskExecutor._tail_log(log_path, max_chars=_MAX_OUTPUT_CHARS + 4096)
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
    def _tail_log(log_path: str, *, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
        path = Path(log_path)
        if not path.exists() or path.is_symlink() or not path.is_file():
            return ""
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_chars * 4))
                value = handle.read(max_chars * 4).decode("utf-8", errors="replace")
        except OSError:
            return ""
        return value[-max_chars:]

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
        # Pi receives one provider credential by design, so its raw stream is
        # untrusted data.  Never copy a credential-shaped value into the
        # answer, artifact, audit payload, or parent-worker transcript.
        safe_output = self._redact_text(output or "")
        safe_terminal_event = dict(terminal_event or {}) if terminal_event else None
        if safe_terminal_event and safe_terminal_event.get("text"):
            safe_terminal_event["text"] = self._redact_text(str(safe_terminal_event["text"]))
        workspace_changes = _workspace_changes(
            Path(str(attempt.get("workspace_path") or "")),
            str(attempt.get("workspace_mode") or ""),
        )
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
        elif exit_code == 0:
            # The exit marker is the verdict, which is the whole reason it is
            # appended by the shell rather than inferred from the log. This
            # used to require terminal_event["status"] == "success" -- a field
            # Pi does not emit, so a clean run was recorded as a failure.
            status = "succeeded"
        else:
            status = "failed"

        # task_manage(action="revise") on a pi child cancels the
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
                    "output_tail": safe_output[-_MAX_OUTPUT_CHARS:],
                    "terminal_event": safe_terminal_event,
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
            )
            return self.start(task["id"], owner=owner, enqueue_poll=enqueue_poll)

        now = utcnow().isoformat()
        attempt.update(
            {
                "status": status,
                "exit_code": exit_code,
                "output_tail": safe_output[-_MAX_OUTPUT_CHARS:],
                "terminal_event": safe_terminal_event,
                "forced_reason": forced_reason,
                "updated_at": now,
            }
        )
        self.store.update_task_attempt(attempt)

        principal_id = task["envelope"]["principal_id"]
        # Pi's final assistant text, when the stream closed cleanly. The raw
        # tail is only a fallback for a crashed or killed run: in JSON mode
        # the last 4000 characters are the middle of one repeated message
        # object, not anything a reader can use.
        answer = (
            (safe_terminal_event or {}).get("text")
            or safe_output[-4000:]
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
                    "terminal_event": safe_terminal_event,
                    "output": safe_output[-_MAX_OUTPUT_CHARS:],
                    "workspace": workspace_changes,
                },
                scope_id=scope_id,
                task_id=task["id"],
                created_by=principal_id,
                metadata={
                    "attempt_id": attempt["id"],
                    "task_id": task["id"],
                    "workspace_mode": attempt.get("workspace_mode"),
                    "workspace_path": attempt.get("workspace_path"),
                },
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
                "workspace": workspace_changes,
            },
            principal_id=principal_id,
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
        )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _future(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def _workspace_changes(root: Path, mode: str) -> dict[str, Any]:
    """Capture bounded evidence of what Pi left in its private workspace."""
    if not root or not root.exists():
        return {"mode": mode, "path": str(root), "status": "missing"}
    if mode == "git_worktree":
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return {
            "mode": mode,
            "path": str(root),
            "status": status.stdout[-20_000:],
            "diff_stat": diff.stdout[-20_000:],
        }
    files = [
        str(item.relative_to(root))
        for item in sorted(root.rglob("*"))
        if not item.is_symlink() and item.is_file() and ".pi" not in item.parts
    ]
    return {"mode": mode, "path": str(root), "files": files[:500], "truncated": len(files) > 500}
