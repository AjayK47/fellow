from toda.harness.prompts import CORE_SYSTEM_PROMPT, render_voice


def test_toda_is_a_colleague_with_a_configurable_name():
    voice = render_voice("Mio")
    assert "You are Mio, a colleague on this team" in voice
    prompt = CORE_SYSTEM_PROMPT.lower()
    assert "requested outcome" in prompt
    assert "never invent" in prompt


def test_company_claims_still_require_authorized_company_evidence():
    prompt = CORE_SYSTEM_PROMPT.lower()
    assert "organization, destination, and scope" in prompt
    assert "untrusted reference material" in prompt


def test_session_manager_delegates_substantial_research_and_report_work():
    prompt = CORE_SYSTEM_PROMPT.lower()
    assert "substantial work" in prompt
    assert "plan" in prompt
    assert "delegate" in prompt
    assert "child" in prompt


def test_substantial_work_uses_lightweight_plans_and_verifies_external_state():
    prompt = CORE_SYSTEM_PROMPT.lower()
    assert "verify" in prompt
    assert "workspace_publish" in prompt
    assert "complete only when" in prompt
