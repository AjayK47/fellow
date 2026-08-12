"""Chart and diagram rendering as self-contained SVG artifacts.

Both tools follow the same contract as the rest of the typed suite: they take
structured input, never a host filesystem path, and return an artifact id
rather than pasting the rendered payload back into the model's context.

SVG rather than PNG is deliberate. The `concept-diagrams` and
`architecture-diagram` skill packages already instruct the model to *hand-write*
SVG ("draw arrows before the component boxes so boxes render on top", "every
<text> element needs an explicit font-size"), which is exactly the work these
tools remove: a chart's axis scaling, tick selection, and label placement are
deterministic geometry that a language model should never be computing by eye.
SVG also stays text, so it embeds directly in `pdf_create`'s HTML and renders
inline from the artifact download route.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from base64 import b64decode
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from toda.harness.models import ToolDefinition
from toda.harness.typed_tools import ArtifactToolBase

_CHART_TYPES = ("bar", "line", "pie", "scatter")

# Categorical palette chosen for distinguishability at small sizes and in
# grayscale print; matplotlib's default cycle is fine but not colorblind-safe
# across its first four entries, which is where most charts live.
_PALETTE = (
    "#2f6f9f",
    "#d1793a",
    "#4f9d6b",
    "#a8556f",
    "#7a6ba8",
    "#8a7256",
)

_DIAGRAM_ENGINES = ("dot", "neato", "fdp", "circo", "twopi")
_DIAGRAM_TIMEOUT_SECONDS = 15
_MAX_DIAGRAM_SOURCE_CHARS = 20_000
_MAX_INFOGRAPHIC_SOURCE_CHARS = 40_000
_MAX_INFOGRAPHIC_SVG_CHARS = 4_000_000
_MAX_INFOGRAPHIC_PNG_BYTES = 16_000_000
_INFOGRAPHIC_FORMATS = ("svg", "png", "both")

# Graphviz can read files off the host through attributes that take a path
# (`image=`, `shapefile=`, `fontpath=`, `imagepath=`). Nothing legitimate in a
# generated architecture diagram needs them, and the renderer runs inside the
# gateway process's own filesystem, so they are refused outright rather than
# sanitized -- a partial escape of a path syntax is not a security boundary.
_DIAGRAM_FORBIDDEN = re.compile(
    r"\b(?:[A-Za-z]*URL|href|target|stylesheet|src|image|imagepath|"
    r"imagescale|shapefile|fontpath|epsf)\s*=",
    re.IGNORECASE,
)

_INFOGRAPHIC_SOURCE_FORBIDDEN = re.compile(
    r"(?:<\s*(?:script|iframe|object|embed|foreignObject)\b|"
    r"(?:https?|file|ftp|javascript|data)\s*:|"
    r"\bon[a-z]+\s*=)",
    re.IGNORECASE,
)
_SVG_FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "foreignObject"}


class VisualTools(ArtifactToolBase):
    """chart_create and diagram_render, both artifact-out and SVG-only."""

    def __init__(self, artifacts: Any) -> None:
        super().__init__(artifacts)
        self._graphviz_path = shutil.which("dot")
        runtime_root = Path(__file__).with_name("infographic_runtime")
        self._infographic_renderer_path = runtime_root / "render.mjs"
        self._infographic_node_path = shutil.which("node")
        self._infographic_package_path = runtime_root / "node_modules" / "@antv" / "infographic"

    # ---- definitions ----

    def definitions(self) -> list[ToolDefinition]:
        graphviz_ready = bool(self._graphviz_path)
        infographic_ready = bool(
            self._infographic_node_path
            and self._infographic_renderer_path.is_file()
            and self._infographic_package_path.is_dir()
        )
        return [
            ToolDefinition(
                name="chart_create",
                description=(
                    "Render company data as an SVG chart artifact (bar, line, "
                    "pie, or scatter) with correctly scaled axes, ticks, and "
                    "labels. Use this instead of describing numbers in prose "
                    "when a comparison, trend, or breakdown is the point, and "
                    "instead of hand-writing SVG. Share the returned artifact "
                    "with slack_send_artifact or embed it in pdf_create's HTML."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "chart_type": {"type": "string", "enum": list(_CHART_TYPES)},
                        "title": {"type": "string"},
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 200,
                            "description": (
                                "Category names, one per value in each series. "
                                "Required for bar and pie."
                            ),
                        },
                        "series": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "values": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 1,
                                        "maxItems": 200,
                                    },
                                    "x": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "maxItems": 200,
                                        "description": (
                                            "Numeric x positions. Required for "
                                            "scatter; optional for line."
                                        ),
                                    },
                                },
                                "required": ["values"],
                                "additionalProperties": False,
                            },
                        },
                        "x_label": {"type": "string"},
                        "y_label": {"type": "string"},
                    },
                    "required": ["chart_type", "title", "series"],
                    "additionalProperties": False,
                },
                # Read risk, unlike pdf_create/html_create. Rendering has no
                # external effect: the artifact is written into the Task's own
                # resolved write scope from data already in context, and the
                # gate that actually matters -- disclosure -- lives on
                # slack_send_artifact, which re-authorizes the scope before it
                # leaves. Tiering this as a write would send every chart
                # through ai_review (and a human pause in supervised mode) for
                # a picture of numbers the model was about to state in prose
                # anyway.
                risk="read",
                capability_id="chart.create",
                max_result_chars=4_000,
            ),
            ToolDefinition(
                name="diagram_render",
                description=(
                    "Render Graphviz DOT source into an SVG diagram artifact "
                    "(architecture, flow, dependency, or sequence-style "
                    "graphs) with automatic layout. Mermaid is not supported; "
                    "write DOT. Prefer this over hand-written SVG whenever "
                    "nodes and edges are the content, since node placement and "
                    "edge routing are solved here rather than estimated."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Complete Graphviz DOT source.",
                        },
                        "title": {"type": "string"},
                        "engine": {
                            "type": "string",
                            "enum": list(_DIAGRAM_ENGINES),
                            "description": (
                                "Layout engine; 'dot' (hierarchical) is the "
                                "right default for architecture and flow."
                            ),
                        },
                    },
                    "required": ["source", "title"],
                    "additionalProperties": False,
                },
                risk="read",
                capability_id="diagram.render",
                runtime_ready=graphviz_ready,
                setup_needed=[] if graphviz_ready else ["graphviz"],
                max_result_chars=4_000,
            ),
            ToolDefinition(
                name="infographic_create",
                description=(
                    "Render concise AntV Infographic DSL into scoped SVG and/or PNG "
                    "artifacts. Use for polished status cards, checklists, timelines, "
                    "comparisons, hierarchies, and narrative data summaries. Prefer "
                    "chart_create for precise multi-series quantitative charts and "
                    "diagram_render for technical node-edge diagrams."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 200},
                        "syntax": {
                            "type": "string",
                            "maxLength": _MAX_INFOGRAPHIC_SOURCE_CHARS,
                            "description": (
                                "Complete AntV Infographic DSL beginning with "
                                "'infographic <template-name>'."
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": list(_INFOGRAPHIC_FORMATS),
                            "description": "Output format; defaults to both.",
                        },
                    },
                    "required": ["title", "syntax"],
                    "additionalProperties": False,
                },
                risk="read",
                capability_id="infographic.create",
                runtime_ready=infographic_ready,
                setup_needed=(
                    []
                    if infographic_ready
                    else ["node", "@antv/infographic", "@resvg/resvg-js"]
                ),
                max_result_chars=4_000,
            ),
        ]

    # ---- dispatch ----

    def execute(self, arguments: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
        name = str(runtime.get("tool_name") or "")
        if name == "chart_create":
            return self._chart_create(arguments, runtime)
        if name == "diagram_render":
            return self._diagram_render(arguments, runtime)
        if name == "infographic_create":
            return self._infographic_create(arguments, runtime)
        raise KeyError(f"unknown visual tool: {name!r}")

    # ---- infographic ----

    def _infographic_create(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        if not (
            self._infographic_node_path
            and self._infographic_renderer_path.is_file()
            and self._infographic_package_path.is_dir()
        ):
            return {
                "status": "setup_needed",
                "operation": "infographic_create",
                "required_capabilities": [
                    "node",
                    "@antv/infographic",
                    "@resvg/resvg-js",
                ],
                "message": (
                    "Install the pinned infographic runtime dependencies in "
                    "src/toda/harness/infographic_runtime."
                ),
            }

        title = str(arguments.get("title") or "Infographic").strip()
        syntax = str(arguments.get("syntax") or "")
        output_format = str(arguments.get("format") or "both")
        if not title:
            raise ValueError("title is empty")
        if output_format not in _INFOGRAPHIC_FORMATS:
            raise ValueError(f"unsupported infographic format: {output_format!r}")
        if not syntax.strip().startswith("infographic "):
            raise ValueError(
                "infographic syntax must begin with 'infographic <template-name>'"
            )
        if len(syntax) > _MAX_INFOGRAPHIC_SOURCE_CHARS:
            raise ValueError(
                f"infographic syntax exceeds {_MAX_INFOGRAPHIC_SOURCE_CHARS} characters"
            )
        if _INFOGRAPHIC_SOURCE_FORBIDDEN.search(syntax):
            raise ValueError(
                "infographic syntax may not contain markup, event handlers, or external resources"
            )

        rendered = self._render_infographic(syntax, include_png=output_format != "svg")
        svg = str(rendered.get("svg") or "")
        self._validate_rendered_svg(svg)

        common_metadata = {
            "operation": "infographic_create",
            "renderer": "@antv/infographic",
            "source_syntax": syntax,
        }
        svg_result = self._create(
            runtime=runtime,
            artifact_type="image.svg",
            title=title,
            content={"text": svg, "mime_type": "image/svg+xml"},
            metadata={**common_metadata, "format": "svg"},
        )
        if output_format == "svg":
            return {**svg_result, "svg_artifact_id": svg_result["artifact_id"]}

        try:
            png = b64decode(str(rendered.get("png_base64") or ""), validate=True)
        except Exception as exc:
            raise ValueError("infographic renderer returned invalid PNG data") from exc
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("infographic renderer did not return a real PNG")
        if len(png) > _MAX_INFOGRAPHIC_PNG_BYTES:
            raise ValueError(
                f"rendered infographic PNG exceeds {_MAX_INFOGRAPHIC_PNG_BYTES} bytes"
            )
        import base64

        png_result = self._create(
            runtime=runtime,
            artifact_type="image.png",
            title=f"{title} preview",
            content={
                "base64": base64.b64encode(png).decode("ascii"),
                "mime_type": "image/png",
            },
            metadata={
                **common_metadata,
                "format": "png",
                "source_artifact_id": svg_result["artifact_id"],
            },
        )
        if output_format == "png":
            return {
                **png_result,
                "svg_artifact_id": svg_result["artifact_id"],
                "png_artifact_id": png_result["artifact_id"],
            }
        return {
            "status": "completed",
            "artifact_id": png_result["artifact_id"],
            "artifact_type": "image.png",
            "svg_artifact_id": svg_result["artifact_id"],
            "png_artifact_id": png_result["artifact_id"],
            "artifacts": [svg_result, png_result],
        }

    def _render_infographic(
        self, syntax: str, *, include_png: bool
    ) -> dict[str, Any]:
        assert self._infographic_node_path is not None
        try:
            completed = subprocess.run(
                [self._infographic_node_path, str(self._infographic_renderer_path)],
                input=json.dumps({"syntax": syntax, "include_png": include_png}),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("infographic renderer timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:2_000]
            raise ValueError(f"infographic renderer failed: {detail or 'unknown error'}")
        try:
            output = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("infographic renderer returned invalid JSON") from exc
        if not isinstance(output, dict):
            raise ValueError("infographic renderer returned an invalid result")
        return output

    @staticmethod
    def _validate_rendered_svg(svg: str) -> None:
        if not svg.lstrip().startswith("<svg"):
            raise ValueError("infographic renderer did not return an SVG document")
        if len(svg) > _MAX_INFOGRAPHIC_SVG_CHARS:
            raise ValueError(
                f"rendered infographic SVG exceeds {_MAX_INFOGRAPHIC_SVG_CHARS} characters"
            )
        if "<!DOCTYPE" in svg.upper() or "<!ENTITY" in svg.upper():
            raise ValueError("rendered infographic SVG contains a forbidden declaration")
        try:
            root = ElementTree.fromstring(svg)
        except ElementTree.ParseError as exc:
            raise ValueError("rendered infographic SVG is malformed") from exc
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise ValueError("rendered infographic root element is not svg")
        for element in root.iter():
            local_tag = element.tag.rsplit("}", 1)[-1]
            if local_tag in _SVG_FORBIDDEN_TAGS:
                raise ValueError(f"rendered infographic contains forbidden {local_tag}")
            for name, value in element.attrib.items():
                local_name = name.rsplit("}", 1)[-1].lower()
                lowered = str(value).strip().lower()
                if local_name.startswith("on"):
                    raise ValueError("rendered infographic contains an event handler")
                if local_name in {"href", "src"} and not (
                    lowered.startswith("#") or lowered.startswith("data:image/")
                ):
                    raise ValueError("rendered infographic contains an external resource")
                if "url(" in lowered:
                    references = re.findall(r"url\(([^)]+)\)", lowered)
                    if any(not item.strip(" '\"").startswith("#") for item in references):
                        raise ValueError("rendered infographic contains an external CSS resource")

    # ---- chart ----

    @staticmethod
    def _series_values(series: dict[str, Any]) -> list[float]:
        values = [float(value) for value in series.get("values") or []]
        if not values:
            raise ValueError("each series needs at least one value")
        return values

    def _chart_create(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        # Imported lazily and used through the object-oriented API rather than
        # pyplot: pyplot keeps global figure state, and tool handlers can run
        # concurrently in the agent loop's read pool.
        import matplotlib
        from matplotlib.backends.backend_svg import FigureCanvasSVG
        from matplotlib.figure import Figure

        chart_type = str(arguments["chart_type"])
        if chart_type not in _CHART_TYPES:
            raise ValueError(f"unsupported chart_type: {chart_type!r}")
        title = str(arguments["title"])
        labels = [str(item) for item in arguments.get("labels") or []]
        series = list(arguments.get("series") or [])
        if not series:
            raise ValueError("at least one series is required")

        figure = Figure(figsize=(9.0, 5.0), dpi=100)
        FigureCanvasSVG(figure)
        axes = figure.subplots()

        if chart_type == "pie":
            self._draw_pie(axes, series[0], labels)
        elif chart_type == "bar":
            self._draw_bar(axes, series, labels)
        elif chart_type == "line":
            self._draw_line(axes, series, labels)
        else:
            self._draw_scatter(axes, series)

        axes.set_title(title, fontsize=13, fontweight="bold", pad=14)
        if chart_type != "pie":
            if arguments.get("x_label"):
                axes.set_xlabel(str(arguments["x_label"]), fontsize=10)
            if arguments.get("y_label"):
                axes.set_ylabel(str(arguments["y_label"]), fontsize=10)
            axes.grid(axis="y", color="#d8dce2", linewidth=0.7)
            axes.set_axisbelow(True)
            for edge in ("top", "right"):
                axes.spines[edge].set_visible(False)
        if len(series) > 1 and chart_type != "pie":
            axes.legend(frameon=False, fontsize=9)
        figure.tight_layout()

        buffer = io.StringIO()
        # Without a pinned hashsalt matplotlib derives its internal element ids
        # from a per-process random salt, and without suppressing Date it
        # stamps the current time -- so the same chart rendered twice produces
        # different bytes, which defeats content hashing and makes a diff of
        # two runs unreadable. rcParams is process-global, but every call here
        # writes the identical constant, so concurrent renders cannot disagree.
        with matplotlib.rc_context({"svg.hashsalt": "toda"}):
            figure.savefig(
                buffer,
                format="svg",
                metadata={"Date": None},
                bbox_inches="tight",
            )
        svg = buffer.getvalue()

        return self._create(
            runtime=runtime,
            artifact_type="image.svg",
            title=title,
            content={"text": svg, "mime_type": "image/svg+xml"},
            metadata={
                "operation": "chart_create",
                "chart_type": chart_type,
                "series_count": len(series),
                "point_count": sum(len(item.get("values") or []) for item in series),
            },
        )

    def _draw_bar(
        self, axes: Any, series: list[dict[str, Any]], labels: list[str]
    ) -> None:
        first = self._series_values(series[0])
        if not labels:
            labels = [str(index + 1) for index in range(len(first))]
        positions = range(len(labels))
        width = 0.8 / len(series)
        for index, item in enumerate(series):
            values = self._series_values(item)
            if len(values) != len(labels):
                raise ValueError("each series must have one value per label")
            offset = (index - (len(series) - 1) / 2) * width
            axes.bar(
                [position + offset for position in positions],
                values,
                width=width,
                label=str(item.get("name") or f"Series {index + 1}"),
                color=_PALETTE[index % len(_PALETTE)],
            )
        _apply_tick_labels(axes, labels)

    def _draw_line(
        self, axes: Any, series: list[dict[str, Any]], labels: list[str]
    ) -> None:
        for index, item in enumerate(series):
            values = self._series_values(item)
            x_values = [float(value) for value in item.get("x") or []]
            if x_values and len(x_values) != len(values):
                raise ValueError("x must have the same length as values")
            axes.plot(
                x_values or list(range(len(values))),
                values,
                marker="o",
                markersize=4,
                linewidth=2,
                label=str(item.get("name") or f"Series {index + 1}"),
                color=_PALETTE[index % len(_PALETTE)],
            )
        if labels:
            longest = max(len(self._series_values(item)) for item in series)
            if len(labels) != longest:
                raise ValueError("labels must have one entry per point")
            _apply_tick_labels(axes, labels)

    def _draw_scatter(self, axes: Any, series: list[dict[str, Any]]) -> None:
        for index, item in enumerate(series):
            values = self._series_values(item)
            x_values = [float(value) for value in item.get("x") or []]
            if len(x_values) != len(values):
                raise ValueError("scatter requires an x array matching values")
            axes.scatter(
                x_values,
                values,
                s=36,
                label=str(item.get("name") or f"Series {index + 1}"),
                color=_PALETTE[index % len(_PALETTE)],
                edgecolors="white",
                linewidths=0.6,
            )

    def _draw_pie(
        self, axes: Any, series: dict[str, Any], labels: list[str]
    ) -> None:
        values = self._series_values(series)
        if any(value < 0 for value in values):
            raise ValueError("pie values must not be negative")
        if sum(values) <= 0:
            raise ValueError("pie values must sum to more than zero")
        if not labels:
            labels = [f"Slice {index + 1}" for index in range(len(values))]
        if len(labels) != len(values):
            raise ValueError("pie needs one label per value")
        axes.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            colors=[_PALETTE[index % len(_PALETTE)] for index in range(len(values))],
            textprops={"fontsize": 9},
            wedgeprops={"edgecolor": "white", "linewidth": 1.2},
        )
        axes.set_aspect("equal")

    # ---- diagram ----

    def _diagram_render(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._graphviz_path:
            return {
                "status": "setup_needed",
                "operation": "diagram_render",
                "required_capabilities": ["graphviz"],
                "message": (
                    "Install Graphviz (the `dot` binary) to render diagrams."
                ),
            }
        source = str(arguments["source"] or "")
        if not source.strip():
            raise ValueError("source is empty")
        if len(source) > _MAX_DIAGRAM_SOURCE_CHARS:
            raise ValueError(
                f"source exceeds {_MAX_DIAGRAM_SOURCE_CHARS} characters"
            )
        forbidden = _DIAGRAM_FORBIDDEN.search(source)
        if forbidden:
            raise ValueError(
                f"DOT attribute {forbidden.group(0).split('=', 1)[0].strip()!r} "
                "can reference host or active external content and is not permitted"
            )
        engine = str(arguments.get("engine") or "dot")
        if engine not in _DIAGRAM_ENGINES:
            raise ValueError(f"unsupported engine: {engine!r}")

        try:
            completed = subprocess.run(
                [self._graphviz_path, f"-K{engine}", "-Tsvg"],
                input=source.encode("utf-8"),
                capture_output=True,
                timeout=_DIAGRAM_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"diagram layout exceeded {_DIAGRAM_TIMEOUT_SECONDS}s"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise ValueError(f"graphviz rejected the source: {detail[:500]}")

        svg = completed.stdout.decode("utf-8", "replace")
        title = str(arguments["title"])
        return self._create(
            runtime=runtime,
            artifact_type="image.svg",
            title=title,
            content={"text": svg, "mime_type": "image/svg+xml"},
            metadata={
                "operation": "diagram_render",
                "engine": engine,
                "source_chars": len(source),
            },
        )


def _apply_tick_labels(axes: Any, labels: list[str]) -> None:
    """Place category labels, rotating only when they would otherwise collide.

    Rotating unconditionally makes short labels ("Q1", "EMEA") harder to read
    for no reason; never rotating overlaps them into illegibility as soon as
    there are many categories or long names.
    """
    rotated = len(labels) > 6 or any(len(label) > 8 for label in labels)
    axes.set_xticks(list(range(len(labels))))
    axes.set_xticklabels(
        labels,
        rotation=45 if rotated else 0,
        ha="right" if rotated else "center",
    )
