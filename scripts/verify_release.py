#!/usr/bin/env python3
"""Fail when the checkout is not safe and complete enough to release."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "LICENSE",
    "README.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "SUPPORT.md",
    "GOVERNANCE.md",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "Dockerfile",
    "compose.yaml",
    ".dockerignore",
    ".env.example",
    "pyproject.toml",
    "uv.lock",
}
FORBIDDEN_PREFIXES = ("data/", "harnesses/", "old/", "build/", ".venv/", "web/dist/")
FORBIDDEN_FILES = {".env", "--help"}
SECRET_SUFFIXES = ("_API_KEY", "_API_KEYS", "_TOKEN", "_SECRET")
OLD_BRANDS = re.compile(r"\b(?:docket|apdate)\b", re.IGNORECASE)


def tracked_existing_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, text=True, capture_output=True
    )
    return [ROOT / item for item in result.stdout.splitlines() if (ROOT / item).is_file()]


def main() -> int:
    errors: list[str] = []
    for relative in sorted(REQUIRED):
        if not (ROOT / relative).is_file():
            errors.append(f"missing required release file: {relative}")

    for path in tracked_existing_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in FORBIDDEN_FILES or relative.startswith(FORBIDDEN_PREFIXES):
            errors.append(f"private/generated path is tracked: {relative}")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if project.get("project", {}).get("name") != "toda-ai":
        errors.append("Python distribution must be named toda-ai")
    scripts = project.get("project", {}).get("scripts", {})
    if scripts.get("toda") != "toda.main:main":
        errors.append("the toda CLI entry point is missing")

    for skill in sorted((ROOT / "src/toda/builtin_skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        license_match = re.search(r"^license:\s*([^\n]+)$", text, re.MULTILINE)
        if not license_match or license_match.group(1).strip() not in {"MIT", "Apache-2.0"}:
            errors.append(f"skill has no reviewed license: {skill.relative_to(ROOT)}")
        if not re.search(r"^origin:\s*$", text, re.MULTILINE):
            errors.append(f"skill has no provenance origin: {skill.relative_to(ROOT)}")

    for line_number, line in enumerate(
        (ROOT / ".env.example").read_text(encoding="utf-8").splitlines(), start=1
    ):
        key, separator, value = line.partition("=")
        if separator and key.endswith(SECRET_SUFFIXES) and value.strip():
            errors.append(f"example secret must be blank: .env.example:{line_number}")

    maintained = [ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "src/toda", ROOT / "web/src"]
    for candidate in maintained:
        paths = [candidate] if candidate.is_file() else candidate.rglob("*")
        for path in paths:
            if path.is_file() and path.suffix.lower() in {".py", ".md", ".ts", ".tsx", ".html", ".css"}:
                if OLD_BRANDS.search(path.read_text(encoding="utf-8", errors="ignore")):
                    errors.append(f"old product name remains: {path.relative_to(ROOT)}")

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    if '"127.0.0.1:8080:8080"' not in compose:
        errors.append("Compose must bind the default port to loopback")

    if errors:
        print("Release verification failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Release verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
