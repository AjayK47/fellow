"""File-backed immutable skill packages.

The database stores a materialized, tenant-aware index of a package, but the
package directory is the source of truth for built-in and connector skills.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_BODY_BYTES = 256_000
_MAX_RESOURCE_BYTES = 1_000_000


class SkillPackageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str = ""
    description: str
    version: str = "1.0.0"
    license: str = "unspecified"
    origin: dict[str, Any] = Field(default_factory=dict)
    kind: str = "builtin"
    platform_key: str | None = None
    activation: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: dict[str, Any] = Field(default_factory=dict)
    response_validation: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("skill name must be lowercase slug-like text")
        return value


class SkillPackage(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: SkillPackageManifest
    root: Path
    body: str
    content_hash: str
    package_path: str

    def as_record(self, *, organization_id: str, skill_id: str) -> dict[str, Any]:
        """Return the tenant materialization consumed by the existing resolver."""
        manifest = self.manifest
        platform_key = manifest.platform_key or f"{manifest.kind}.{manifest.name}"
        return {
            "id": skill_id,
            "organization_id": organization_id,
            "name": manifest.display_name or manifest.name,
            "description": manifest.description,
            "goal": manifest.description,
            "version": 1,
            "status": "approved",
            "inputs": list(manifest.inputs),
            "preconditions": list(manifest.preconditions),
            "steps": list(manifest.steps),
            "warnings": list(manifest.warnings),
            "activation_mode": (manifest.activation or {}).get("mode", "optional"),
            "activation": {
                key: value
                for key, value in (manifest.activation or {}).items()
                if key != "mode"
            },
            "response_validation": dict(manifest.response_validation),
            "platform_key": platform_key,
            "kind": manifest.kind,
            "immutable": manifest.kind in {"builtin", "connector"},
            "instructions": self.body,
            "content_hash": self.content_hash,
            "package_path": self.package_path,
            "license": manifest.license,
            "origin": dict(manifest.origin),
            "required_capabilities": dict(manifest.required_capabilities),
            "owner_id": None,
            "scope_id": "scope_org_public",
        }


class SkillPackageCatalog:
    """Scan, validate, and resolve immutable package directories."""

    def __init__(self, roots: list[Path] | None = None) -> None:
        default_root = Path(__file__).resolve().parents[1] / "builtin_skills"
        self.roots = roots or [default_root]
        self._packages: dict[str, SkillPackage] = {}
        self.reload()

    def reload(self) -> None:
        packages: dict[str, SkillPackage] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for skill_file in sorted(root.rglob("SKILL.md")):
                package = self._load(skill_file)
                if package.manifest.name in packages:
                    raise ValueError(f"duplicate skill package: {package.manifest.name}")
                packages[package.manifest.name] = package
        self._packages = packages

    def list(self) -> list[SkillPackage]:
        return [self._packages[name] for name in sorted(self._packages)]

    def get(self, name: str) -> SkillPackage | None:
        return self._packages.get(name)

    def metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "name": package.manifest.name,
                "display_name": package.manifest.display_name or package.manifest.name,
                "description": package.manifest.description,
                "version": package.manifest.version,
                "kind": package.manifest.kind,
                "license": package.manifest.license,
                "origin": package.manifest.origin,
                "platform_key": package.manifest.platform_key,
                "required_capabilities": package.manifest.required_capabilities,
                "content_hash": package.content_hash,
            }
            for package in self.list()
        ]

    def readiness(self, available_capabilities: set[str] | None = None) -> list[dict[str, Any]]:
        """Return deterministic capability readiness for every package.

        A package may declare ``all_of`` and/or ``any_of`` capability names.
        Missing optional integrations make a skill unavailable; they never
        silently change its instructions or tool permissions.
        """
        available = available_capabilities or set()
        result: list[dict[str, Any]] = []
        for package in self.list():
            required = package.manifest.required_capabilities or {}
            all_of = {str(item) for item in required.get("all_of", [])}
            any_of = {str(item) for item in required.get("any_of", [])}
            missing = sorted(all_of - available)
            any_satisfied = not any_of or bool(any_of & available)
            ready = not missing and any_satisfied
            result.append(
                {
                    "name": package.manifest.name,
                    "ready": ready,
                    "required_capabilities": required,
                    "missing_capabilities": missing + ([] if any_satisfied else ["one_of:" + ",".join(sorted(any_of))]),
                    "content_hash": package.content_hash,
                }
            )
        return result

    def resource(self, name: str, relative_path: str) -> dict[str, Any]:
        package = self._packages.get(name)
        if not package:
            raise KeyError(name)
        candidate = (package.root / relative_path).resolve()
        root = package.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("skill resource escapes package root")
        if candidate.name == "SKILL.md":
            raise ValueError("use the skill package body for SKILL.md")
        if not candidate.is_file():
            raise FileNotFoundError(relative_path)
        size = candidate.stat().st_size
        if size > _MAX_RESOURCE_BYTES:
            raise ValueError("skill resource exceeds size limit")
        return {
            "skill": name,
            "path": relative_path,
            "content": candidate.read_text(encoding="utf-8"),
            "content_hash": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }

    @staticmethod
    def _load(skill_file: Path) -> SkillPackage:
        raw = skill_file.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > _MAX_BODY_BYTES + 32_000:
            raise ValueError(f"skill package is too large: {skill_file}")
        if not raw.startswith("---\n"):
            raise ValueError(f"skill package has no YAML frontmatter: {skill_file}")
        end = raw.find("\n---", 4)
        if end < 0:
            raise ValueError(f"skill package has unterminated frontmatter: {skill_file}")
        frontmatter = yaml.safe_load(raw[4:end]) or {}
        body = raw[end + 4 :].lstrip("\n")
        if not isinstance(frontmatter, dict):
            raise ValueError(f"skill frontmatter must be a mapping: {skill_file}")
        manifest = SkillPackageManifest.model_validate(frontmatter)
        if manifest.kind not in {"builtin", "connector", "marketplace"}:
            raise ValueError(f"unsupported immutable skill kind: {manifest.kind}")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return SkillPackage(
            manifest=manifest,
            root=skill_file.parent,
            body=body,
            content_hash=digest,
            package_path=str(skill_file),
        )
