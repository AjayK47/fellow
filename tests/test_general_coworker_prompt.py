from toda.harness.prompts import STABLE_SYSTEM_PROMPT


def test_toda_is_a_general_coworker_with_additional_company_context():
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
    assert "discover relevant approved skills first" in prompt
    assert "extract the requested outcomes" in prompt
    assert "actually connected systems" in prompt
    assert prompt.index("discover relevant approved skills first") < prompt.index(
        "decide whether a self-contained substantial piece belongs to a child"
    )
    assert "call delegate_tasks before doing that piece yourself" in prompt
    assert "current-event research plus analysis" in prompt
    assert "one general specialist may own an entire sequential workflow" in prompt
    assert "directly handle short conversational replies" in prompt


def test_substantial_work_uses_lightweight_plans_and_verifies_external_state():
    prompt = STABLE_SYSTEM_PROMPT.lower()
    assert "lightweight plan" in prompt
    assert "plans are durable checklists, not workflow programs" in prompt
    assert "provider accepted" in prompt
    assert "claim verified completion only after checking" in prompt
    assert "never ask which app" in prompt
