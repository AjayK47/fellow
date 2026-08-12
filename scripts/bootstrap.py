#!/usr/bin/env python3
"""Create a secure local Toda configuration without overwriting one."""

from __future__ import annotations

import argparse
import secrets
import stat
from pathlib import Path


SECRET_KEYS = {
    "TODA_API_KEYS": lambda: secrets.token_urlsafe(32),
    "TODA_DASHBOARD_SESSION_SECRET": lambda: secrets.token_urlsafe(48),
    "TODA_LOCAL_ADMIN_TOKEN": lambda: secrets.token_urlsafe(32),
}


def bootstrap(example: Path, output: Path) -> dict[str, str]:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing configuration: {output}")
    values = {key: factory() for key, factory in SECRET_KEYS.items()}
    lines: list[str] = []
    found: set[str] = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        key, separator, _value = line.partition("=")
        if separator and key in values:
            lines.append(f"{key}={values[key]}")
            found.add(key)
        else:
            lines.append(line)
    missing = set(values) - found
    if missing:
        raise ValueError(f"Example configuration is missing: {', '.join(sorted(missing))}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return values


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Create Toda's local .env securely")
    parser.add_argument("--example", type=Path, default=root / ".env.example")
    parser.add_argument("--output", type=Path, default=root / ".env")
    args = parser.parse_args()
    values = bootstrap(args.example, args.output)
    print(f"Created {args.output} with mode 0600.")
    print("Local administrator token (store it in your password manager):")
    print(values["TODA_LOCAL_ADMIN_TOKEN"])


if __name__ == "__main__":
    main()
