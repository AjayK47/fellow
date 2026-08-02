#!/usr/bin/env python3
"""Inspect a Docket agent run: print the full transcript showing, per turn,
which tools were offered, whether Docket answered from retrieved context
(no tool calls) or invoked tools, and what each tool returned.

Usage:
    conda run -n kys python scripts/inspect_run.py                # list recent runs
    conda run -n kys python scripts/inspect_run.py <run_id>       # inspect one run
    conda run -n kys python scripts/inspect_run.py <run_id> -n 40 # longer message history
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(
    os.environ.get("DOCKET_DB")
    or Path(
        os.environ.get("DOCKET_DATA_DIR", Path.cwd() / "data")
    )
    / os.environ.get("DOCKET_DB_NAME", "brain.db")
)


def _de(text: str | None) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


def _trim(text: object, limit: int) -> str:
    s = str(text or "")
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + " …"


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"database not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(conn: sqlite3.Connection, org: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT payload FROM agent_runs
           WHERE organization_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (org, limit),
    ).fetchall()
    return [_de(r["payload"]) for r in rows]


def child_runs(conn: sqlite3.Connection, parent_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT payload FROM agent_runs
           WHERE payload LIKE ? ORDER BY created_at ASC""",
        (f'%"parent_run_id": "{parent_id}"%',),
    ).fetchall()
    return [_de(r["payload"]) for r in rows]


def run_messages(conn: sqlite3.Connection, session_id: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT payload FROM agent_messages
           WHERE session_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (session_id, limit),
    ).fetchall()
    return list(reversed([_de(r["payload"]) for r in rows]))


def run_turns(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT payload FROM agent_turns
           WHERE run_id = ? ORDER BY turn_number ASC""",
        (run_id,),
    ).fetchall()
    return [_de(r["payload"]) for r in rows]


def tool_calls(conn: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT payload FROM harness_tool_calls
           WHERE run_id = ? ORDER BY created_at ASC""",
        (run_id,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        call = _de(r["payload"])
        if call.get("id"):
            out[call["id"]] = call
    return out


def artifacts(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT payload FROM artifacts
           WHERE payload LIKE ? ORDER BY created_at ASC""",
        (f'%"run_id": "{run_id}"%',),
    ).fetchall()
    return [_de(r["payload"]) for r in rows]


def print_header(run: dict) -> None:
    print("=" * 78)
    print(f"RUN {run.get('id')}")
    print(f"  goal       : {_trim(run.get('goal'), 140)}")
    print(f"  state      : {run.get('state')}")
    print(f"  trigger    : {run.get('trigger_kind')}")
    print(f"  principal  : {run.get('envelope', {}).get('principal_id')}")
    print(f"  trace_id   : {run.get('trace_id')}")
    print(f"  session    : {run.get('session_id')}")
    print(f"  parent_run : {run.get('parent_run_id') or '— (main agent)'}")
    meta = run.get("metadata") or {}
    if meta.get("specialist_worker"):
        print(f"  specialist : role={meta.get('worker_role')} depth={meta.get('worker_depth')}")
    if meta.get("slack"):
        sl = meta["slack"]
        print(f"  slack      : channel={sl.get('channel_id')} thread={sl.get('thread_id')}")
    print(f"  iterations : {run.get('iteration_count')} / {run.get('max_iterations')}")
    print(f"  tool_calls : {run.get('tool_call_count')} / {run.get('max_tool_calls')}")
    usage = run.get("usage") or {}
    if usage:
        print(
            f"  usage      : model_calls={usage.get('model_calls',0)} "
            f"prompt={usage.get('prompt_tokens',0)} "
            f"completion={usage.get('completion_tokens',0)} "
            f"total={usage.get('total_tokens',0)} cost={usage.get('cost',0)}"
        )
    print(f"  created    : {run.get('created_at')}")
    print(f"  updated    : {run.get('updated_at')}")
    answer = run.get("final_answer")
    if answer:
        print(f"  final_answer ({len(answer)} chars):")
        print("    " + _trim(answer, 2000).replace("\n", "\n    "))
    print("=" * 78)


def print_turn(turn: dict, idx: int, calls_by_id: dict[str, dict]) -> None:
    print(f"\n--- TURN {idx} ---")
    req = turn.get("request") or {}
    resp = turn.get("response") or {}
    offered = req.get("tools") or []
    print(f"  tools offered ({len(offered)}): {', '.join(offered) or '—'}")
    model = resp.get("model") or ""
    usage = resp.get("usage") or {}
    print(
        f"  model={model} "
        f"finish={resp.get('finish_reason')} "
        f"prompt_tokens={usage.get('prompt_tokens','-')} "
        f"completion_tokens={usage.get('completion_tokens','-')}"
    )
    tool_calls = resp.get("tool_calls") or []
    if not tool_calls:
        print("  >> NO TOOL CALLS — Docket answered from retrieved context/memory alone")
        content = resp.get("content") or ""
        if content:
            print("  assistant content:")
            print("    " + _trim(content, 1200).replace("\n", "\n    "))
        return
    print(f"  >> {len(tool_calls)} TOOL CALL(S):")
    for call in tool_calls:
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name")
        args = fn.get("arguments") if fn else call.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError:
            pass
        call_id = call.get("id")
        print(f"    -> {name}({json.dumps(args, default=str)[:200]})")
        tc = calls_by_id.get(call_id)
        if tc:
            status = tc.get("status")
            print(f"       status={status}")
            out = tc.get("output")
            if out is not None:
                print("       output: " + _trim(out, 1000).replace("\n", "\n       "))
            err = tc.get("error")
            if err:
                print("       error: " + _trim(err, 600))
        else:
            print("       (no persisted tool-call record)")


def print_message(msg: dict) -> None:
    role = msg.get("role")
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    name = msg.get("name") or ""
    if role == "user":
        print(f"\n[USER] {_trim(content, 1400)}")
    elif role == "assistant":
        label = f"[ASSISTANT{name and f'/{name}'}]"
        print(f"\n{label} {_trim(content, 1400)}")
        if tool_calls:
            for call in tool_calls:
                fn = call.get("function") or {}
                print(
                    f"     tool_call: {fn.get('name')}("
                    + _trim(fn.get("arguments"), 160)
                    + ")"
                )
    elif role == "tool":
        print(f"\n[TOOL/{name}] {_trim(content, 1400)}")
    else:
        print(f"\n[{role.upper()}] {_trim(content, 400)}")


def inspect(conn: sqlite3.Connection, run_id: str, msg_limit: int) -> int:
    run = _de(conn.execute(
        "SELECT payload FROM agent_runs WHERE id = ?", (run_id,)
    ).fetchone()["payload"])
    if not run:
        sys.exit(f"run not found: {run_id}")
    print_header(run)
    children = child_runs(conn, run_id)
    if children:
        print(f"\n### SPECIALIST WORKERS ({len(children)}):")
        for child in children:
            cmeta = child.get("metadata") or {}
            print(
                f"  - {child['id']} role={cmeta.get('worker_role')} "
                f"state={child.get('state')} iter={child.get('iteration_count')} "
                f"tools={child.get('tool_call_count')}"
            )
            print(f"    goal: {_trim(child.get('goal'), 200)}")
            ans = child.get("final_answer")
            if ans:
                print(f"    result: {_trim(ans, 400)}")
    arts = artifacts(conn, run_id)
    if arts:
        print(f"\n### ARTIFACTS ({len(arts)}):")
        for art in arts:
            print(
                f"  - {art.get('id')} type={art.get('artifact_type')} "
                f"title={_trim(art.get('title'), 100)}"
            )
    calls_by_id = tool_calls(conn, run_id)
    turns = run_turns(conn, run_id)
    if turns:
        print(f"\n### TURNS (model loop, {len(turns)}):")
        for i, turn in enumerate(turns, 1):
            print_turn(turn, i, calls_by_id)
    else:
        print("\n### TURNS: (none persisted)")
    session_id = run.get("session_id")
    if session_id:
        msgs = run_messages(conn, session_id, msg_limit)
        print(f"\n### MESSAGE TRANSCRIPT ({len(msgs)} most recent):")
        for msg in msgs:
            print_message(msg)
    print("\n" + "=" * 78)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a Docket agent run.")
    parser.add_argument("run_id", nargs="?", help="run id to inspect; omit to list recent runs")
    parser.add_argument("--org", default=os.environ.get("DOCKET_DEFAULT_ORG_ID", "org_acme"))
    parser.add_argument("--limit", type=int, default=20, help="number of recent runs to list")
    parser.add_argument("-n", "--messages", type=int, default=40, help="message history limit")
    args = parser.parse_args()
    conn = connect()
    try:
        if args.run_id:
            return inspect(conn, args.run_id, args.messages)
        runs = list_runs(conn, args.org, args.limit)
        if not runs:
            print(f"no runs found for org={args.org}")
            return 1
        print(f"Recent runs (org={args.org}, limit={args.limit}):\n")
        for r in runs:
            print(
                f"{r.get('id'):40} {r.get('state'):14} "
                f"{r.get('trigger_kind'):10} iter={r.get('iteration_count')} "
                f"tools={r.get('tool_call_count')} "
                f"{r.get('created_at','')}  "
                f"{_trim(r.get('goal'), 80)}"
            )
        print(f"\nInspect with: python scripts/inspect_run.py <run_id>")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())