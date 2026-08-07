from docket.harness.prompts import STABLE_SYSTEM_PROMPT


def test_docket_is_a_general_coworker_with_additional_company_context():
    prompt = STABLE_SYSTEM_PROMPT.lower()
    assert "general-purpose coworker" in prompt
    assert "do not limit you to company-only questions" in prompt
    assert "normal reasoning and" in prompt
    assert "public or" in prompt and "internal-company interpretation" in prompt
    assert "use only the supplied permission-filtered context and tools" not in prompt


def test_company_claims_still_require_authorized_company_evidence():
    prompt = STABLE_SYSTEM_PROMPT.lower()
    assert "for company-specific claims" in prompt
    assert "permission-filtered company context" in prompt
    assert "missing company evidence" in prompt
    assert "does not prevent an unrelated public-world answer" in prompt


def test_session_manager_delegates_substantial_research_and_report_work():
    prompt = STABLE_SYSTEM_PROMPT.lower()
    assert "you are the session manager for root tasks" in prompt
    assert "call delegate_tasks before doing that piece yourself" in prompt
    assert "current-event research plus analysis" in prompt
    assert "one general specialist may own an entire sequential workflow" in prompt
    assert "directly handle short conversational replies" in prompt
