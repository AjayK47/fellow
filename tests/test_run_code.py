"""H-07 (rescoped): the sandboxed run_code tool — Python-only subprocess,
callable only into a fixed read-only tool allow-list, resource-limited,
secret-scrubbed environment, approval-gated at registration."""

from __future__ import annotations

import os

from docket.harness.run_code import (
    RUN_CODE_CANDIDATE_TOOLS,
    RunCodeSandbox,
    generate_stub_module,
    scrub_child_env,
)
from docket.harness.tools import build_tool_registry


class _FakeDefinition:
    def __init__(self, risk: str) -> None:
        self.risk = risk


class _FakeRegistered:
    def __init__(self, handler, risk: str = "read") -> None:
        self.definition = _FakeDefinition(risk)
        self.handler = handler


class _FakeRegistry:
    def __init__(self, tools: dict[str, _FakeRegistered]) -> None:
        self._tools = tools

    def get(self, name: str):
        return self._tools.get(name)


def test_scrub_child_env_removes_secret_shaped_names_and_keeps_safe_ones():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/docket",
        "OPENAI_API_KEY": "sk-should-not-leak",
        "DOCKET_SLACK_BOT_TOKEN": "xoxb-should-not-leak",
        "DATABASE_PASSWORD": "hunter2",
        "SOME_WEBHOOK_URL": "https://internal/hook",
        "RANDOM_APP_SETTING": "keep-me",
    }
    scrubbed = scrub_child_env(source)
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["HOME"] == "/home/docket"
    assert "OPENAI_API_KEY" not in scrubbed
    assert "DOCKET_SLACK_BOT_TOKEN" not in scrubbed
    assert "DATABASE_PASSWORD" not in scrubbed
    assert "SOME_WEBHOOK_URL" not in scrubbed
    # Not on the safe-prefix allowlist and not secret-shaped -> dropped by
    # default (matches the tool's "no ambient environment" design), only
    # PATH/HOME/LANG/etc-prefixed names pass through.
    assert "RANDOM_APP_SETTING" not in scrubbed


def test_generate_stub_module_only_exposes_named_tools():
    source = generate_stub_module(["brain_search", "brain_list_cases"])
    assert "def brain_search(**kwargs):" in source
    assert "def brain_list_cases(**kwargs):" in source
    assert "def run_code(" not in source
    assert "SALLY_RPC_SOCKET" in source and "SALLY_RPC_TOKEN" in source


def test_sandbox_runs_a_trivial_script_and_captures_stdout():
    sandbox = RunCodeSandbox(registry=_FakeRegistry({}), allowed_tools=(), timeout_seconds=10, max_tool_calls=5)
    result = sandbox.run("print('hello from sandbox')", runtime={})
    assert result["status"] == "success"
    assert "hello from sandbox" in result["output"]
    assert result["exit_code"] == 0
    assert result["tool_calls_made"] == 0


def test_sandbox_dispatches_an_allowed_read_tool_callback():
    registry = _FakeRegistry(
        {"echo_tool": _FakeRegistered(lambda args, rt: {"echo": args.get("text")})}
    )
    sandbox = RunCodeSandbox(
        registry=registry, allowed_tools=("echo_tool",), timeout_seconds=10, max_tool_calls=5
    )
    result = sandbox.run(
        "import docket_tools\nprint(docket_tools.echo_tool(text='hi'))", runtime={"organization_id": "org_acme"}
    )
    assert result["status"] == "success"
    assert result["tool_calls_made"] == 1
    assert "'echo': 'hi'" in result["output"]


def test_sandbox_rejects_a_tool_not_on_the_allow_list():
    registry = _FakeRegistry(
        {"echo_tool": _FakeRegistered(lambda args, rt: {"echo": "x"})}
    )
    sandbox = RunCodeSandbox(
        registry=registry, allowed_tools=("echo_tool",), timeout_seconds=10, max_tool_calls=5
    )
    # The stub module is only generated for allowed_tools, so an
    # out-of-allow-list name isn't even a callable attribute on it.
    result = sandbox.run(
        "import docket_tools\nprint(docket_tools.brain_query_facts())", runtime={}
    )
    assert result["status"] == "error"
    assert "no attribute" in result["output"] or "AttributeError" in result["output"]


def test_sandbox_blocks_a_write_risk_tool_even_if_misconfigured_onto_the_allow_list():
    """Defense in depth: the per-call risk re-check must hold even if the
    allow-list itself were ever misconfigured to include a non-read tool."""
    registry = _FakeRegistry(
        {"shell_like_tool": _FakeRegistered(lambda args, rt: {"ran": True}, risk="write")}
    )
    sandbox = RunCodeSandbox(
        registry=registry,
        allowed_tools=("shell_like_tool",),
        timeout_seconds=10,
        max_tool_calls=5,
    )
    result = sandbox.run(
        "import docket_tools\nprint(docket_tools.shell_like_tool())", runtime={}
    )
    assert result["status"] == "success"
    assert "not read-safe" in result["output"]


def test_sandbox_enforces_the_tool_call_limit():
    calls = {"count": 0}

    def _handler(args, rt):
        calls["count"] += 1
        return {"n": calls["count"]}

    registry = _FakeRegistry({"echo_tool": _FakeRegistered(_handler)})
    sandbox = RunCodeSandbox(
        registry=registry, allowed_tools=("echo_tool",), timeout_seconds=10, max_tool_calls=2
    )
    script = (
        "import docket_tools\n"
        "for _ in range(4):\n"
        "    print(docket_tools.echo_tool())\n"
    )
    result = sandbox.run(script, runtime={})
    assert result["tool_calls_made"] == 2
    assert "tool call limit reached" in result["output"]


def test_sandbox_kills_a_script_that_exceeds_the_timeout():
    sandbox = RunCodeSandbox(registry=_FakeRegistry({}), allowed_tools=(), timeout_seconds=1, max_tool_calls=5)
    result = sandbox.run("import time\ntime.sleep(5)", runtime={})
    assert result["status"] == "timeout"
    assert "timed out" in result["error"]


def test_sandbox_child_process_does_not_inherit_secret_shaped_env_vars(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-into-sandbox")
    sandbox = RunCodeSandbox(registry=_FakeRegistry({}), allowed_tools=(), timeout_seconds=10, max_tool_calls=5)
    result = sandbox.run(
        "import os\nprint(os.environ.get('OPENAI_API_KEY', 'ABSENT'))", runtime={}
    )
    assert result["status"] == "success"
    assert "ABSENT" in result["output"]
    assert "sk-should-not-leak" not in result["output"]


def test_run_code_is_registered_by_default_and_approval_gated(brain):
    registered = brain.tool_registry.get("run_code")
    assert registered is not None
    assert registered.definition.risk == "write"
    assert registered.definition.approval_required is True


def test_run_code_registers_with_read_only_intersection_and_is_approval_gated(brain):
    registry = build_tool_registry(
        store=brain.store,
        retrieval=brain.retrieval,
        context=brain.context,
        skills=brain.skills,
        composio=brain.composio,
        reality=brain.reality,
        artifacts=brain.artifacts,
        web_tools=brain.web_tools,
        run_code_enabled=True,
        run_code_timeout_seconds=5,
        run_code_max_tool_calls=3,
    )
    registered = registry.get("run_code")
    assert registered is not None
    assert registered.definition.risk == "write"
    assert registered.definition.approval_required is True
    for name in RUN_CODE_CANDIDATE_TOOLS:
        if name in registered.definition.description:
            assert registry.get(name) is not None
            assert registry.get(name).definition.risk == "read"


def test_run_code_end_to_end_calls_back_into_a_real_read_tool(brain):
    registry = build_tool_registry(
        store=brain.store,
        retrieval=brain.retrieval,
        context=brain.context,
        skills=brain.skills,
        composio=brain.composio,
        reality=brain.reality,
        artifacts=brain.artifacts,
        web_tools=brain.web_tools,
        run_code_enabled=True,
        run_code_timeout_seconds=10,
        run_code_max_tool_calls=5,
    )
    run_code = registry.get("run_code")
    runtime = {
        "organization_id": "org_acme",
        "principal_id": "person_ajay",
        "destination": {"type": "internal_agent_session", "id": "test", "is_private": True},
        "purpose": "reactive_agent",
        "task_id": "task_test_run_code",
    }
    script = (
        "import docket_tools\n"
        "result = docket_tools.brain_search(query='Acme export')\n"
        "print(result.get('count'))\n"
    )
    output = run_code.handler({"code": script}, runtime)
    assert output["status"] == "success"
    assert output["output"].strip().isdigit()
    assert output["tool_calls_made"] == 1
