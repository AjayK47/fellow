from pathlib import Path

import pytest

from scripts.bootstrap import SECRET_KEYS, bootstrap


def test_bootstrap_generates_secrets_and_never_overwrites(tmp_path: Path):
    example = tmp_path / ".env.example"
    output = tmp_path / ".env"
    example.write_text("\n".join(f"{key}=" for key in SECRET_KEYS) + "\n")
    values = bootstrap(example, output)
    text = output.read_text()
    assert all(f"{key}={value}" in text for key, value in values.items())
    assert all(len(value) >= 32 for value in values.values())
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        bootstrap(example, output)
