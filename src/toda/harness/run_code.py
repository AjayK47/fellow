"""H-07 (rescoped): a scoped `run_code` tool modeled on Hermes's
`execute_code` — not a full sandbox/computer.

The LLM writes Python; it runs in a child process with a stripped environment.
The intended capability is calling back into a small, fixed allow-list of
Toda's own existing **read** tools over a local Unix domain socket. This is
not an OS sandbox: without a configured low-privilege/container backend the
process can still inspect anything readable by the Toda service account.
Hard limits: a timeout, a max tool-call count, and a bounded stdout capture.

Cautionary lesson taken from Hermes's own bug tracker
(NousResearch/hermes-agent#4146): a sandboxed script reaching a shell/terminal
tool through the same callback mechanism bypassed their normal
command-approval flow. Toda's allow-list contains no shell-like tool at
all, so that specific bypass class doesn't exist here — the callback
dispatcher (`_handle_request`) additionally re-verifies every call is both
on the allow-list *and* declared `risk == "read"` on every single request,
not just once at setup, so a future addition to the allow-list can't
silently regain that capability without also being read-only.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Env var name-based scrubbing (distinct from services/redaction.py, which
# scans text *content* for secret-shaped values). This blocks the *whole
# variable* by name so the child process never inherits it in the first
# place, regardless of what it's used for.
_SAFE_ENV_PREFIXES = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LOGNAME",
)
_SECRET_ENV_SUBSTRINGS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "CREDS",
    "AUTH",
    "DSN",
    "WEBHOOK",
    "BEARER",
    "APIKEY",
)

# The candidate set run_code may call back into. build_tool_registry()
# intersects this with whichever of these are actually registered and
# risk="read" (some depend on optional services like `reality`/`artifacts`),
# so run_code never advertises a callback that doesn't exist or isn't safe.
RUN_CODE_CANDIDATE_TOOLS = (
    "memory_search",
    "memory_query_facts",
    "case_list",
    "judgment_guidance",
    "procedure_find",
    "artifact_list",
)

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_TOOL_CALLS = 20
_MAX_OUTPUT_CHARS = 20_000
_SOCKET_DIR = "/tmp" if sys.platform == "darwin" else tempfile.gettempdir()


def scrub_child_env(source_env: dict[str, str]) -> dict[str, str]:
    scrubbed: dict[str, str] = {}
    for key, value in source_env.items():
        if any(marker in key.upper() for marker in _SECRET_ENV_SUBSTRINGS):
            continue
        if key.startswith(_SAFE_ENV_PREFIXES):
            scrubbed[key] = value
    return scrubbed


def generate_stub_module(allowed_tools: list[str]) -> str:
    """Build the source of the `toda_tools` module the child script imports.

    Each allowed tool becomes a plain `**kwargs` function so this doesn't
    need to introspect every tool's JSON schema into a Python signature —
    the child just calls `memory_search(query="...")` and the kwargs are
    forwarded as-is to the real tool's argument validation on the parent
    side, which rejects anything malformed exactly like a normal tool call.
    """
    functions = []
    for name in allowed_tools:
        functions.append(
            f"def {name}(**kwargs):\n"
            f"    return _call({name!r}, kwargs)\n"
        )
    return (
        '"""Auto-generated Toda tool stubs for run_code — read-only callbacks."""\n'
        "import json, os, socket, threading\n\n"
        '_SOCK_PATH = os.environ["SALLY_RPC_SOCKET"]\n'
        '_TOKEN = os.environ["SALLY_RPC_TOKEN"]\n'
        "_sock = None\n"
        "# The server accepts a single persistent connection and answers many\n"
        "# requests over it (not one connection per call), and has no\n"
        "# request-id in the protocol — serialize the whole round-trip so\n"
        "# concurrent calls from multiple threads can't interleave and steal\n"
        "# each other's responses.\n"
        "_call_lock = threading.Lock()\n\n\n"
        "def _connect():\n"
        "    global _sock\n"
        "    if _sock is None:\n"
        "        _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "        _sock.connect(_SOCK_PATH)\n"
        "    return _sock\n\n\n"
        "def _call(tool_name, args):\n"
        "    with _call_lock:\n"
        "        conn = _connect()\n"
        '        request = json.dumps({"tool": tool_name, "args": args, "token": _TOKEN}) + "\\n"\n'
        "        conn.sendall(request.encode())\n"
        '        buf = b""\n'
        '        while not buf.endswith(b"\\n"):\n'
        "            chunk = conn.recv(65536)\n"
        "            if not chunk:\n"
        '                raise RuntimeError("Toda sandbox connection closed")\n'
        "            buf += chunk\n"
        "    return json.loads(buf.decode())\n\n\n"
        + "\n\n".join(functions)
        + "\n"
    )


class RunCodeSandbox:
    """Runs one script per call. Not a persistent computer — no state, no
    container, no lifecycle beyond the single subprocess this spawns.

    **The name is aspirational: this is not an isolation boundary.** The
    script is launched with `subprocess.Popen([sys.executable, ...])` under
    Toda's own operating-system account. What it actually constrains is:

    * the working directory (a fresh temp dir),
    * environment variables (`scrub_child_env` drops anything that looks
      like a secret and keeps only a safe prefix list),
    * which Toda tools it may call back into (read-risk only, re-verified
      per call on the parent side),
    * wall-clock time and output size.

    What it does NOT constrain, and cannot: absolute filesystem paths, the
    network, other processes, or resource usage. A script can read
    `/etc/hosts`, list the operator's home directory, open a socket to any
    host, and read the Toda database file directly. This was verified, not
    assumed.

    Consequently `tools.run_code_enabled` defaults to False. Hosted
    deployments pass an AgentRunner so the child runs as the dedicated agent
    account; development-only callers may omit it, in which case the real
    host-account blast radius remains explicit in the tool description.
    """

    def __init__(
        self,
        *,
        registry: Any,
        allowed_tools: tuple[str, ...],
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS,
        runner: Any | None = None,
    ) -> None:
        self.registry = registry
        self.allowed_tools = frozenset(allowed_tools)
        self.timeout_seconds = timeout_seconds
        self.max_tool_calls = max_tool_calls
        self.runner = runner

    def run(self, code: str, *, runtime: dict[str, Any]) -> dict[str, Any]:
        if not code or not code.strip():
            return {"status": "error", "error": "No code provided.", "tool_calls_made": 0}

        tmpdir = tempfile.mkdtemp(prefix="toda_run_code_", dir=_SOCKET_DIR)
        sock_path = os.path.join(tmpdir, "rpc.sock")
        token = secrets.token_urlsafe(24)
        tool_call_count = [0]
        stop_event = threading.Event()

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        os.chmod(sock_path, 0o600)
        server_sock.listen(1)
        server_sock.settimeout(0.2)

        server_thread = threading.Thread(
            target=self._serve,
            args=(server_sock, stop_event, token, tool_call_count, runtime),
            daemon=True,
        )
        server_thread.start()

        try:
            with open(os.path.join(tmpdir, "toda_tools.py"), "w", encoding="utf-8") as handle:
                handle.write(generate_stub_module(sorted(self.allowed_tools)))
            with open(os.path.join(tmpdir, "script.py"), "w", encoding="utf-8") as handle:
                handle.write(code)

            child_env = scrub_child_env(dict(os.environ))
            child_env["SALLY_RPC_SOCKET"] = sock_path
            child_env["SALLY_RPC_TOKEN"] = token
            child_env["PYTHONDONTWRITEBYTECODE"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"

            start = time.monotonic()
            status = "success"
            stdout_text = ""
            exit_code = -1
            output_path = os.path.join(tmpdir, "stdout.log")
            output_handle = open(output_path, "wb")
            try:
                if self.runner is not None:
                    proc = self.runner.start(
                        shell_command=shlex.join([sys.executable, "script.py"]),
                        cwd=Path(tmpdir),
                        env=child_env,
                        stdout=output_handle,
                        stderr=subprocess.STDOUT,
                    )
                else:
                    proc = subprocess.Popen(
                        [sys.executable, "script.py"],
                        cwd=tmpdir,
                        env=child_env,
                        stdout=output_handle,
                        stderr=subprocess.STDOUT,
                    )
                try:
                    proc.wait(timeout=self.timeout_seconds)
                    exit_code = proc.returncode
                    if exit_code != 0:
                        status = "error"
                except subprocess.TimeoutExpired:
                    # AgentRunner starts a new session so a timed-out script
                    # cannot leave grandchildren running after the temporary
                    # directory is removed.  Plain development callers still
                    # get the ordinary Popen kill fallback.
                    if self.runner is not None:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (OSError, ProcessLookupError):
                            proc.kill()
                    else:
                        proc.kill()
                    proc.wait()
                    status = "timeout"
            except Exception as exc:
                status = "error"
                stdout_text = f"{stdout_text}\n{exc}" if stdout_text else str(exc)
            finally:
                output_handle.close()

            if not stdout_text:
                stdout_text = self._read_output_tail(output_path)

            duration = round(time.monotonic() - start, 2)
            result: dict[str, Any] = {
                "status": status,
                "output": stdout_text[:_MAX_OUTPUT_CHARS],
                "exit_code": exit_code,
                "tool_calls_made": tool_call_count[0],
                "duration_seconds": duration,
            }
            if len(stdout_text) > _MAX_OUTPUT_CHARS:
                result["output_truncated"] = True
                result["output_total_characters"] = len(stdout_text)
            if status == "timeout":
                result["error"] = f"Script timed out after {self.timeout_seconds}s and was killed."
            return result
        finally:
            stop_event.set()
            try:
                server_sock.close()
            except OSError:
                pass
            server_thread.join(timeout=2)
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _read_output_tail(path: str) -> str:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _MAX_OUTPUT_CHARS * 4))
                value = handle.read(_MAX_OUTPUT_CHARS * 4).decode("utf-8", errors="replace")
            return value[-_MAX_OUTPUT_CHARS:]
        except OSError:
            return ""

    def _serve(
        self,
        server_sock: socket.socket,
        stop_event: threading.Event,
        token: str,
        tool_call_count: list[int],
        runtime: dict[str, Any],
    ) -> None:
        conn: socket.socket | None = None
        try:
            deadline = time.monotonic() + self.timeout_seconds
            while not stop_event.is_set() and time.monotonic() < deadline:
                try:
                    conn, _addr = server_sock.accept()
                    break
                except socket.timeout:
                    continue
                except OSError:
                    return
            if conn is None:
                return
            conn.settimeout(self.timeout_seconds)
            buf = b""
            while not stop_event.is_set():
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._handle_request(line, token, tool_call_count, runtime)
                    try:
                        conn.sendall((response + "\n").encode())
                    except OSError:
                        return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_request(
        self,
        line: bytes,
        token: str,
        tool_call_count: list[int],
        runtime: dict[str, Any],
    ) -> str:
        try:
            request = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return json.dumps({"error": f"invalid request: {exc}"})
        if not secrets.compare_digest(str(request.get("token") or ""), token):
            return json.dumps({"error": "unauthorized"})
        if tool_call_count[0] >= self.max_tool_calls:
            return json.dumps(
                {"error": f"tool call limit reached ({self.max_tool_calls})"}
            )
        tool_name = str(request.get("tool") or "")
        if tool_name not in self.allowed_tools:
            available = ", ".join(sorted(self.allowed_tools))
            return json.dumps(
                {"error": f"tool '{tool_name}' is not available in run_code. Available: {available}"}
            )
        registered = self.registry.get(tool_name)
        # Re-verify read-only on every call, not just once when the
        # allow-list was chosen — the allow-list plus this per-call check
        # together are what keep a future non-read tool addition from
        # silently becoming callable from inside the sandbox.
        if not registered or registered.definition.risk != "read":
            return json.dumps(
                {"error": f"tool '{tool_name}' is not read-safe and cannot be called from run_code"}
            )
        tool_call_count[0] += 1
        try:
            output = registered.handler(request.get("args") or {}, runtime)
            return json.dumps(output, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
