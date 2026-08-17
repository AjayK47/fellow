# Changelog

All notable changes are recorded here. Toda follows Semantic Versioning after
1.0; before 1.0, minor releases may contain documented breaking changes.

## Unreleased

### Added

- Toda Community open-source release baseline.
- Docker and loopback-only Compose deployment.
- Local administrator-token login for self-hosted installations.
- Bearer authentication for non-local event ingestion.
- Release verification, CI, security scanning, and community governance files.

### Changed

- Python distribution name is `toda-ai`; the import package and CLI remain
  `toda`.
- Model-generated code execution is disabled by default.

### Security

- Network-bound startup rejects disabled dashboard authentication and automatic
  write approval.
