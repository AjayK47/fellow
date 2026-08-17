"""The deterministic backstop below the action reviewer.

Adapted from OpenMausBot's auto-approve guard (MIT). Its own framing is the
right one: this is not a security boundary, it is a "you probably did not
mean to hand THIS one over unattended" check. Toda has no sandbox for
`run_code` or Pi, so it is currently the only thing between an injected
instruction and the host -- which is exactly why its limits are asserted
here alongside its coverage.
"""

from __future__ import annotations

import pytest

from toda.harness.hazard import (
    hazard_reason,
    looks_destructive,
    looks_sensitive,
    touches_toda_state,
)


@pytest.mark.parametrize(
    "code",
    [
        "import os; os.system('rm -rf /tmp/x')",
        "subprocess.run(['dd', 'if=/dev/zero', 'of=/dev/sda'])",
        "os.system('git push --force origin main')",
        "cur.execute('DROP TABLE customers')",
        "shutil.rmtree('/var/data')",
    ],
)
def test_destructive_commands_are_caught(code):
    assert looks_destructive(code)


@pytest.mark.parametrize(
    "code",
    [
        "open('/Users/me/.aws/credentials').read()",
        "print(open('.env').read())",
        "Path.home().joinpath('.ssh/id_rsa').read_text()",
        "os.system('security find-generic-password -s github')",
    ],
)
def test_credential_reads_are_caught(code):
    assert looks_sensitive(code)


def test_todas_own_state_is_the_worst_case_and_is_named_first():
    """Not in the upstream list, because OpenMausBot has no equivalent: one
    SQLite file holds every memory, transcript, artifact and connector
    credential, at a path a model can guess."""
    code = "import sqlite3; sqlite3.connect('brain.db').execute('select * from memories')"
    assert touches_toda_state(code)
    # Even when the script is ALSO destructive, the worst finding is reported.
    both = "sqlite3.connect('brain.db'); os.system('rm -rf /')"
    assert "database or configuration" in (hazard_reason(both) or "")


def test_ordinary_computation_is_not_flagged():
    """A backstop that fires on normal work is worse than none -- it trains
    people to approve without reading."""
    for code in (
        "print(sum(int(x) for x in data))",
        "import json; print(json.dumps(rows, indent=2))",
        "results = brain_search(query='renewal blockers')",
        "df = [r for r in rows if r['status'] == 'open']",
    ):
        assert hazard_reason(code) is None, code


def test_the_prescan_informs_the_reviewer_rather_than_vetoing():
    """The reviewer is the gate. A prescan hit is one more input to it, not
    a decision -- reading a `.env` in a repo you are debugging is ordinary
    work, and forcing a human pause for it puts someone in the loop for
    scripts they would have approved anyway."""
    from toda.harness.tools import hazard_prescan

    reason = hazard_prescan("run_code", {"code": "open('.env').read()"})
    assert reason and "credentials" in reason
    assert hazard_prescan("run_code", {"code": "print(1+1)"}) is None
    # Scoped to run_code: connector actions have their own classifier.
    assert hazard_prescan("COMPOSIO_EXECUTE", {"code": "open('.env')"}) is None


def test_the_reviewer_is_told_what_the_prescan_found():
    """Signal it would otherwise have to spot unaided in a wall of code."""
    from toda.harness.action_review import ActionReviewer

    seen = {}

    class Recording:
        model = "m"

        def generate(self, *, messages, **_kw):
            seen["payload"] = messages[-1]["content"]
            return type("R", (), {"content": '{"verdict": "approve"}'})()

    reviewer = ActionReviewer(model=Recording())
    reviewer.review(
        tool_name="run_code",
        description="run a script",
        arguments={"code": "open('.env')"},
        hazard="This script reads credentials or private keys.",
    )
    assert "flagged_by_prescan" in seen["payload"]
    assert "credentials" in seen["payload"]


def test_pi_prescan_flags_without_blocking(tmp_path):
    """Pi is a full-trust coding subprocess by design; refusing outright on
    a pattern match would make it useless for the debugging it exists for.
    The reviewer decides."""
    from toda.harness.pi_task_executor import check_hardline_floor, prescan_pi_launch

    # The hardline floor keeps only the one true invariant.
    assert check_hardline_floor(
        "do not push anything", ["pi", "run", "git push"], tmp_path
    )
    assert check_hardline_floor("summarize ~/.aws/credentials", ["pi"], tmp_path) is None

    # The prescan observes, and says so.
    assert prescan_pi_launch("summarize ~/.aws/credentials", ["pi", "run"])
    assert prescan_pi_launch("fix the failing test", ["pi", "run"]) is None
