"""Slack does not speak the Markdown models write by default."""

from __future__ import annotations

from toda.harness.slack_markdown import to_slack_mrkdwn as md


def test_headings_become_bold_lines():
    """Slack has no headings; "### 1. What is Terafab?" rendered literally."""
    assert md("### 1. What is Terafab?") == "*1. What is Terafab?*"
    assert md("# Top\n\nbody") == "*Top*\n\nbody"


def test_bold_loses_a_star_and_italic_keeps_one_underscore():
    assert md("**Terafab** is a fab") == "*Terafab* is a fab"
    assert md("__really__ important") == "_really_ important"
    assert md("***both***") == "*_both_*"


def test_strikethrough_and_links_use_slack_forms():
    assert md("~~gone~~") == "~gone~"
    assert md("[the docs](https://x.test/a)") == "<https://x.test/a|the docs>"
    assert md("![img](https://x.test/i.png)") == "<https://x.test/i.png|img>"


def test_bullets_become_real_bullets():
    assert md("- one\n- two") == "• one\n• two"
    assert md("  * nested") == "  • nested"


def test_code_is_never_rewritten():
    """An asterisk in a shell command is content, not formatting."""
    fenced = "```\nrm -rf **/*.pyc\n```"
    assert md(fenced) == fenced
    assert md("run `ls **` now") == "run `ls **` now"
    mixed = "**bold** then ```\n**not bold**\n```"
    assert md(mixed) == "*bold* then ```\n**not bold**\n```"


def test_the_real_output_that_prompted_this():
    """Verbatim from a production Slack message."""
    raw = (
        "Here is the rundown on the recent buzz around Elon Musk's **Terafab** project:\n\n"
        "### 1. What is Terafab?\n"
        "**Terafab** is a proposed massive semiconductor fabrication plant.\n\n"
        "- Scale: enormous\n"
        "- Status: [announced](https://x.com/elonmusk)\n"
    )
    out = md(raw)
    assert "###" not in out
    assert "**" not in out
    assert "*Terafab*" in out
    assert "*1. What is Terafab?*" in out
    assert "• Scale: enormous" in out
    assert "<https://x.com/elonmusk|announced>" in out


def test_plain_text_is_returned_unchanged():
    assert md("Hey! How can I help you today?") == "Hey! How can I help you today?"
    assert md("") == ""


def test_a_table_divider_row_is_dropped():
    """Slack cannot render tables; the divider is pure noise."""
    out = md("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "---" not in out
    assert "| a | b |" in out


def test_document_tools_are_delegated_not_carried(brain):
    """Twenty document schemas are ~5,300 tokens on every turn -- 40% of the
    tool payload -- shipped even for "how are you doing?". A Session Manager
    deciding whether to answer or delegate does not need pdf_fill_form's
    argument list; the child Task that does the work still gets it."""
    def run(worker: str) -> dict:
        return {"organization_id": "org_acme", "metadata": {"worker_type": worker},
                "envelope": {"destination": {"type": "slack_channel"},
                             "principal_id": "person_ajay"},
                "trigger_kind": "reactive", "parent_task_id": None, "id": "t"}

    manager = brain.tool_executor.allowed_definitions(run("session_manager"))
    child = brain.tool_executor.allowed_definitions(run("general"))

    def names(ds):
        return {d.name for d in ds}

    # Creating a document is a common direct request, so it stays.
    assert "pdf_create" in names(manager)
    # Operating on an existing artifact is specialist work.
    # ocr_extract is left out: its availability depends on an optional
    # dependency, so the readiness gate may exclude it either way.
    for specialist in ("pdf_split", "pdf_merge", "xlsx_edit"):
        assert specialist not in names(manager), specialist
        assert specialist in names(child), specialist
    assert len(manager) < len(child)
