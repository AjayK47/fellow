"""Compatibility command for the published organizational benchmark."""

from __future__ import annotations

import argparse
import json

from benchmarks.organization_1000 import run


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--people", type=int, default=1_000)
    parser.add_argument("--tasks", type=int, default=2_000)
    arguments = parser.parse_args()
    print(json.dumps(run(people=arguments.people, tasks=arguments.tasks), indent=2))
