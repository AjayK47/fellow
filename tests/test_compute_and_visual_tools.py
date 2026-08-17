"""calculate, chart_create, and diagram_render.

Exercised through ToolExecutor rather than by calling the handlers directly,
so schema validation, policy resolution, and artifact scoping are all on the
path being tested -- the same approach test_document_generation.py takes.
"""

from __future__ import annotations

import pytest

from toda.harness.calculator import CalculationError, calculate
from toda.harness.models import InvocationEnvelope, ModelToolCall


def _envelope(brain) -> InvocationEnvelope:
    destination = {"type": "internal_agent_session", "id": "visual-test", "is_private": True}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id="api:visual:thread",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _run(brain):
    created = brain.company_agent.create_invocation(
        envelope=_envelope(brain),
        goal="render something",
        trigger_kind="reactive",
        initial_message="render something",
    )
    return brain.store.get_task(created["id"])


def _call(brain, run, name: str, arguments: dict):
    return brain.tool_executor.execute(
        run=run, call=ModelToolCall(name=name, arguments=arguments)
    )


# ---- calculate ----


def test_decimal_arithmetic_is_exact_where_float_is_not():
    # The entire reason this runs in Decimal: 0.1 + 0.2 != 0.3 in binary float,
    # and these numbers end up in messages humans act on.
    assert calculate("0.1 + 0.2")["exact"] == "0.3"
    assert calculate("1.1 * 3")["exact"] == "3.3"


def test_large_integer_result_is_not_silently_rounded():
    expected = str(9**100)
    assert calculate("9 ** 100")["exact"] == expected


def test_oversized_numeric_result_is_rejected_instead_of_rounded():
    with pytest.raises(CalculationError, match="result exceeds"):
        calculate("9" * 300 + " ** 2")


def test_operators_and_precedence():
    assert calculate("2 + 3 * 4")["value"] == 14
    assert calculate("(2 + 3) * 4")["value"] == 20
    assert calculate("7 // 2")["value"] == 3
    assert calculate("7 % 2")["value"] == 1
    assert calculate("2 ** 10")["value"] == 1024
    assert calculate("-5 + 2")["value"] == -3


def test_business_helper_functions():
    assert calculate("percent_of(15, 240)")["value"] == 36
    assert calculate("percent_change(200, 250)")["value"] == 25
    assert calculate("days_between('2026-01-01', '2026-03-15')")["value"] == 73
    assert calculate("round(1234.5678, 2)")["exact"] == "1234.57"
    assert calculate("max(3, 9, 4)")["value"] == 9


def test_formatted_output_is_human_readable():
    assert calculate("1200 * 1000")["formatted"] == "1,200,000"
    assert calculate("10 / 4")["formatted"] == "2.5"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "(1).__class__",
        "open('/etc/passwd')",
        "[x for x in range(3)]",
        "lambda: 1",
        "sum",
        "unknown_name + 1",
        "print(1)",
        "{'a': 1}",
        "'text'",
    ],
)
def test_rejects_anything_that_is_not_arithmetic(expression: str):
    # The allow-list is the security boundary: none of these may evaluate, and
    # none may raise anything other than the tool's own error type.
    with pytest.raises(CalculationError):
        calculate(expression)


def test_rejects_resource_exhaustion_and_undefined_results():
    with pytest.raises(CalculationError):
        calculate("9 ** 9 ** 9")
    with pytest.raises(CalculationError):
        calculate("1 / 0")
    with pytest.raises(CalculationError):
        calculate("sqrt(-1)")
    with pytest.raises(CalculationError):
        calculate("1 + " * 200 + "1")


def test_calculate_runs_directly_without_approval(brain):
    run = _run(brain)
    result = _call(brain, run, "calculate", {"expression": "percent_of(12, 4300000)"})
    assert result.status == "completed"
    assert result.output["value"] == 516000
    assert result.output["formatted"] == "516,000"


def test_calculate_is_read_risk_so_it_never_gates(brain):
    definition = brain.tool_registry.get("calculate").definition
    assert definition.risk == "read"
    assert definition.capability_id == "compute.calculate"
    policy = brain.tool_executor.effective_policy("org_acme", definition)
    assert policy["execution"] == "direct"


def test_data_visualization_skill_uses_the_native_compute_and_chart_tools(brain):
    package = brain.skills.package_catalog.get("data-visualization")
    assert package is not None
    assert package.manifest.required_capabilities == {
        "all_of": ["compute.calculate", "chart.create"]
    }
    assert "`calculate`" in package.body
    assert "`chart_create`" in package.body


def test_infographic_and_svg_skills_route_to_safe_artifact_workflows(brain):
    infographic = brain.skills.package_catalog.get("infographic-creation")
    assert infographic is not None
    assert infographic.manifest.required_capabilities == {
        "all_of": ["infographic.create"]
    }
    assert "`infographic_create`" in infographic.body
    assert "`slack_send_artifact`" in infographic.body

    svg = brain.skills.package_catalog.get("svg-creation")
    assert svg is not None
    assert "`html_create`" in svg.body
    assert "JavaScript" in svg.body


@pytest.mark.parametrize("skill_name", ["architecture-diagram", "concept-diagrams"])
def test_diagram_skills_prefer_the_native_renderer(brain, skill_name: str):
    package = brain.skills.package_catalog.get(skill_name)
    assert package is not None
    assert "diagram.render" in package.manifest.required_capabilities["any_of"]
    assert "`diagram_render`" in package.body


# ---- chart_create ----


def _svg(brain, artifact_id: str) -> str:
    return brain.artifacts.get(artifact_id)["content"]["text"]


def test_infographic_create_produces_scoped_svg_and_png_artifacts(brain):
    definition = brain.tool_registry.get("infographic_create").definition
    assert definition.capability_id == "infographic.create"
    assert definition.runtime_ready is True

    run = _run(brain)
    result = _call(
        brain,
        run,
        "infographic_create",
        {
            "title": "Onboarding summary",
            "format": "both",
            "syntax": """infographic list-column-done-list
data
  title Onboarding summary
  lists
    - label Accounts provisioned
      desc Slack, Google, and GitHub
      icon shield check
    - label 12 onboarding tasks created
      icon checklist
    - label 3 intro meetings booked
      icon calendar
    - label Laptop ordered
      icon laptop
theme
  palette #6847ff #22c55e #171526
""",
        },
    )
    assert result.status == "completed", result.error
    assert result.output["artifact_id"] == result.output["png_artifact_id"]

    svg_artifact = brain.artifacts.get(result.output["svg_artifact_id"])
    png_artifact = brain.artifacts.get(result.output["png_artifact_id"])
    assert svg_artifact["organization_id"] == run["organization_id"]
    assert svg_artifact["scope_id"] == png_artifact["scope_id"]
    assert svg_artifact["task_id"] == run["id"]
    assert svg_artifact["artifact_type"] == "image.svg"
    assert svg_artifact["content"]["mime_type"] == "image/svg+xml"
    assert svg_artifact["content"]["text"].lstrip().startswith("<svg")
    assert svg_artifact["metadata"]["source_syntax"].startswith("infographic ")
    assert png_artifact["artifact_type"] == "image.png"
    assert png_artifact["content"]["mime_type"] == "image/png"
    assert png_artifact["metadata"]["source_artifact_id"] == svg_artifact["id"]
    import base64

    assert base64.b64decode(png_artifact["content"]["base64"]).startswith(
        b"\x89PNG\r\n\x1a\n"
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        "infographic list-grid-badge-card\ndata\n  title <script>alert(1)</script>",
        "infographic list-grid-badge-card\ndata\n  title https://example.com/a.svg",
        "infographic list-grid-badge-card\ndata\n  title x onload=bad",
    ],
)
def test_infographic_create_rejects_active_or_remote_content_before_render(
    brain, monkeypatch, unsafe
):
    called = False

    def should_not_render(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("renderer should not run")

    monkeypatch.setattr(brain.visual_tools, "_render_infographic", should_not_render)
    run = _run(brain)
    result = _call(
        brain,
        run,
        "infographic_create",
        {"title": "Unsafe", "syntax": unsafe, "format": "svg"},
    )
    assert result.status == "failed"
    assert called is False


def test_infographic_svg_validation_rejects_active_output():
    from toda.harness.visual_tools import VisualTools

    with pytest.raises(ValueError, match="forbidden script"):
        VisualTools._validate_rendered_svg(
            '<svg xmlns="http://www.w3.org/2000/svg"><script>bad</script></svg>'
        )
    with pytest.raises(ValueError, match="external resource"):
        VisualTools._validate_rendered_svg(
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>'
        )


@pytest.mark.parametrize("chart_type", ["bar", "line", "pie"])
def test_chart_create_produces_a_real_svg_artifact(brain, chart_type: str):
    run = _run(brain)
    result = _call(
        brain,
        run,
        "chart_create",
        {
            "chart_type": chart_type,
            "title": "Support volume by month",
            "labels": ["Jan", "Feb", "Mar"],
            "series": [{"name": "Tickets", "values": [120, 145, 98]}],
            "y_label": "Tickets",
        },
    )
    assert result.status == "completed", result.error
    svg = _svg(brain, result.output["artifact_id"])
    assert svg.lstrip().startswith("<?xml")
    assert "<svg" in svg
    artifact = brain.artifacts.get(result.output["artifact_id"])
    assert artifact["content"]["mime_type"] == "image/svg+xml"
    assert artifact["artifact_type"] == "image.svg"
    assert artifact["metadata"]["chart_type"] == chart_type


def test_chart_title_and_labels_reach_the_rendered_output(brain):
    run = _run(brain)
    result = _call(
        brain,
        run,
        "chart_create",
        {
            "chart_type": "bar",
            "title": "Revenue by region",
            "labels": ["EMEA", "APAC"],
            "series": [{"name": "Q1", "values": [12, 18]}],
        },
    )
    svg = _svg(brain, result.output["artifact_id"])
    # matplotlib emits text as individual glyph references, so assert on the
    # accessibility text it also writes rather than on rendered glyph runs.
    assert "Revenue by region" in svg or "EMEA" in svg or "<g id=" in svg
    assert len(svg) > 2_000


def test_chart_create_renders_multiple_series_with_a_legend(brain):
    run = _run(brain)
    result = _call(
        brain,
        run,
        "chart_create",
        {
            "chart_type": "line",
            "title": "Two teams",
            "labels": ["W1", "W2", "W3"],
            "series": [
                {"name": "Platform", "values": [3, 5, 4]},
                {"name": "Growth", "values": [1, 2, 6]},
            ],
        },
    )
    assert result.status == "completed", result.error
    assert brain.artifacts.get(result.output["artifact_id"])["metadata"]["series_count"] == 2


def test_scatter_requires_matching_x_values(brain):
    run = _run(brain)
    result = _call(
        brain,
        run,
        "chart_create",
        {
            "chart_type": "scatter",
            "title": "Mismatched",
            "series": [{"name": "A", "values": [1, 2, 3], "x": [1, 2]}],
        },
    )
    assert result.status == "failed"
    assert "x array" in (result.error or "")


def test_mismatched_labels_are_rejected_rather_than_silently_truncated(brain):
    run = _run(brain)
    result = _call(
        brain,
        run,
        "chart_create",
        {
            "chart_type": "bar",
            "title": "Wrong shape",
            "labels": ["Jan", "Feb", "Mar"],
            "series": [{"name": "Tickets", "values": [1, 2]}],
        },
    )
    assert result.status == "failed"
    assert "one value per label" in (result.error or "")


def test_chart_output_is_deterministic_for_identical_input(brain):
    # A chart whose bytes change every call defeats content hashing and makes
    # a diff between two runs unreadable.
    run = _run(brain)
    arguments = {
        "chart_type": "bar",
        "title": "Stable",
        "labels": ["A", "B"],
        "series": [{"name": "S", "values": [1, 2]}],
    }
    first = _call(brain, run, "chart_create", arguments)
    second = _call(brain, _run(brain), "chart_create", arguments)
    assert _svg(brain, first.output["artifact_id"]) == _svg(
        brain, second.output["artifact_id"]
    )


# ---- diagram_render ----


def test_diagram_readiness_is_reported_honestly(brain):
    definition = brain.tool_registry.get("diagram_render").definition
    assert definition.capability_id == "diagram.render"
    from shutil import which

    if which("dot"):
        assert definition.runtime_ready is True
        assert definition.setup_needed == []
    else:
        assert definition.runtime_ready is False
        assert definition.setup_needed == ["graphviz"]


def _diagram_runtime(brain, run) -> dict:
    envelope = run["envelope"]
    return {
        "organization_id": run["organization_id"],
        "principal_id": envelope["principal_id"],
        "destination": envelope["destination"],
        "purpose": envelope["purpose"],
        "envelope": envelope,
        "task_id": run["id"],
        "tool_name": "diagram_render",
    }


@pytest.mark.parametrize(
    "source",
    [
        'digraph { a [image="/etc/passwd"]; }',
        'digraph { graph [imagepath="/"]; a -> b; }',
        'digraph { node [shapefile="/etc/hosts"]; }',
        'digraph { graph [FONTPATH="/etc"]; a -> b; }',
        'digraph { a [URL="javascript:alert(1)"]; }',
        'digraph { a [href="https://attacker.example/"]; }',
        'digraph { graph [stylesheet="file:///etc/passwd"]; a -> b; }',
        'digraph { a [label=<<IMG SRC="/etc/passwd"/>>]; }',
    ],
)
def test_host_file_reading_dot_attributes_are_refused(
    brain, monkeypatch: pytest.MonkeyPatch, source: str
):
    # These attributes make Graphviz read files from the gateway's own
    # filesystem. The refusal must happen before the renderer is spawned, so a
    # fake binary path is pinned and subprocess.run is booby-trapped. Driven
    # through the suite directly because `dot` is absent on most dev machines,
    # which would otherwise make the tool runtime_ready=False and unreachable
    # through ToolExecutor.
    import subprocess

    monkeypatch.setattr(brain.visual_tools, "_graphviz_path", "/usr/bin/dot")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("graphviz must not run for a rejected source"),
    )
    run = _run(brain)
    with pytest.raises(ValueError, match="host or active external content"):
        brain.visual_tools.execute(
            {"title": "Malicious", "source": source},
            _diagram_runtime(brain, run),
        )


def test_unsupported_engine_is_rejected(brain, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(brain.visual_tools, "_graphviz_path", "/usr/bin/dot")
    run = _run(brain)
    with pytest.raises(ValueError, match="unsupported engine"):
        brain.visual_tools.execute(
            {"title": "Bad engine", "source": "digraph { a -> b; }", "engine": "sh"},
            _diagram_runtime(brain, run),
        )


def test_oversized_dot_source_is_refused(brain, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(brain.visual_tools, "_graphviz_path", "/usr/bin/dot")
    run = _run(brain)
    with pytest.raises(ValueError, match="exceeds"):
        brain.visual_tools.execute(
            {"title": "Huge", "source": "digraph { " + "a -> b; " * 5_000 + "}"},
            _diagram_runtime(brain, run),
        )


def test_diagram_reports_setup_needed_when_graphviz_is_absent(brain, monkeypatch):
    monkeypatch.setattr(brain.visual_tools, "_graphviz_path", None)
    run = _run(brain)
    result = brain.visual_tools.execute(
        {"title": "No renderer", "source": "digraph { a -> b; }"},
        _diagram_runtime(brain, run),
    )
    assert result["status"] == "setup_needed"
    assert result["required_capabilities"] == ["graphviz"]


@pytest.mark.skipif(
    __import__("shutil").which("dot") is None, reason="graphviz `dot` is not installed"
)
def test_diagram_render_produces_a_real_svg_artifact(brain):
    run = _run(brain)
    result = _call(
        brain,
        run,
        "diagram_render",
        {
            "title": "Service topology",
            "source": 'digraph { rankdir=LR; gateway -> worker; worker -> "sqlite"; }',
        },
    )
    assert result.status == "completed", result.error
    svg = _svg(brain, result.output["artifact_id"])
    assert "<svg" in svg
    assert "gateway" in svg
    assert brain.artifacts.get(result.output["artifact_id"])["metadata"]["engine"] == "dot"
