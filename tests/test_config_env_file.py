"""Environment-file discovery for gateway processes."""

from pathlib import Path

from docket.config import settings_env_file


def test_settings_env_file_finds_project_root_from_nested_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    nested = project / "landing" / "src"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'example'\n")
    monkeypatch.chdir(nested)
    monkeypatch.delenv("DOCKET_ENV_FILE", raising=False)

    assert settings_env_file() == project / ".env"


def test_settings_env_file_honors_explicit_override(
    tmp_path: Path, monkeypatch,
) -> None:
    configured = tmp_path / "gateway.env"
    monkeypatch.setenv("DOCKET_ENV_FILE", str(configured))

    assert settings_env_file() == configured
