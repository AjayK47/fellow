"""The Pi CLI contract, checked against the actual binary.

Every other Pi test substitutes `echo` or `sleep` for the real thing, which
is what let a `--prompt` flag that Pi has never accepted sit in the codebase
unnoticed: the command was only ever asserted against a fixture shaped like
itself. These tests invoke `pi` for real, so the two ways Toda can drift
away from it -- the flags it launches with, and the JSON it expects back --
both fail loudly here instead of at the first delegated coding Task.

Skipped when `pi` is not installed. The one test that spends money on a
model call is opt-in via TODA_TEST_PI_LIVE=1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from toda.harness.pi_task_executor import parse_terminal_event, pi_command_builder

pytestmark = pytest.mark.skipif(
    shutil.which("pi") is None, reason="the pi CLI is not installed"
)

_MODEL = "z-ai/glm-5.2"
_PROVIDER = "openrouter"


def _run(args: list[str], *, env: dict[str, str] | None = None, timeout: int = 60):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **(env or {})},
    )


def test_every_flag_the_builder_uses_is_one_pi_accepts():
    """Pi rejects an unknown option outright, so a bad flag is a dead Task.

    Asserted against `--help` rather than by running the command, so this
    stays free and offline while still catching a renamed or removed flag.
    """
    helptext = _run(["pi", "--help"], timeout=30).stdout
    command = pi_command_builder(provider=_PROVIDER, model=_MODEL)("do the thing")

    assert command[0] == "pi"
    assert command[-1] == "do the thing", "the prompt is positional, not a flag value"
    for flag in [arg for arg in command if arg.startswith("--")]:
        assert flag in helptext, f"pi does not accept {flag}"


def test_pi_rejects_an_unknown_flag_rather_than_ignoring_it():
    """Why the test above is worth having: there is no lenient path here.

    The old builder passed `--prompt`, and this is exactly what every Pi
    Task got back -- instantly, with no output and nothing in the log to
    explain it.
    """
    result = _run(["pi", "--prompt", "hello"], timeout=30)
    assert result.returncode != 0
    assert "Unknown option" in result.stderr


def test_the_credential_never_appears_in_argv():
    """`ps` is readable by every process on the host, so the key goes through
    the environment instead -- see PiTaskExecutor.start()."""
    command = pi_command_builder(provider=_PROVIDER, model=_MODEL)("do the thing")
    assert "--api-key" not in command


@pytest.mark.skipif(
    os.environ.get("TODA_TEST_PI_LIVE") != "1",
    reason="spends money on a real model call; set TODA_TEST_PI_LIVE=1 to run",
)
def test_a_real_run_ends_with_an_event_the_parser_understands():
    """The half of the contract `--help` cannot cover: the shape coming back.

    Pi closes with `agent_end` and then a bare `agent_settled`, and reports
    no status field at all -- an earlier parser looked for
    `status in {success, completed}` and so returned None for every real run
    Toda has ever made.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("OPENROUTER_API_KEY is not set")

    command = pi_command_builder(provider=_PROVIDER, model=_MODEL)(
        "Reply with exactly the word OK and nothing else."
    )
    result = _run(command + ["--no-session"], env={"OPENROUTER_API_KEY": key}, timeout=180)
    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert json.loads(lines[-1])["type"] == "agent_settled"

    event = parse_terminal_event(result.stdout)
    assert event is not None, "no agent_end found in a successful run"
    assert event["text"] == "OK"
    assert event["model"] == _MODEL
    assert event["will_retry"] is False
    assert event["usage"]["totalTokens"] > 0
