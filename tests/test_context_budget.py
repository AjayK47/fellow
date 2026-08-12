from toda.harness.context_budget import (
    context_budget_for_request,
    estimate_tokens,
    request_budget,
)
import pytest


def test_request_budget_reserves_output_before_input():
    budget = request_budget(
        context_window=64_000,
        output_tokens=16_000,
        messages=[{"role": "user", "content": "hello"}],
    )
    assert budget.output_reservation == 16_000
    assert budget.effective_input_window == 47_488
    assert budget.fits


def test_tool_schemas_count_as_request_input():
    small = request_budget(
        context_window=64_000,
        output_tokens=4_000,
        messages=[{"role": "user", "content": "hello"}],
    )
    large = request_budget(
        context_window=64_000,
        output_tokens=4_000,
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "x" * 40_000,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert large.estimated_input_tokens > small.estimated_input_tokens


def test_company_context_budget_uses_remaining_request_space():
    budget = context_budget_for_request(
        context_window=8_000,
        output_tokens=2_000,
        messages_without_company_context=[
            {"role": "system", "content": "fixed prompt" * 100}
        ],
        configured_context_budget=10_000,
    )
    assert 0 < budget < 10_000


def test_estimator_includes_json_keys_and_empty_values():
    assert estimate_tokens({"messages": [{"role": "user", "content": "x"}]}) > 1


@pytest.mark.parametrize("window", [64_000, 128_000, 200_000, 512_000, 1_000_000])
def test_standard_request_stays_inside_supported_provider_windows(window):
    budget = request_budget(
        context_window=window,
        output_tokens=min(40_000, window // 4),
        messages=[
            {"role": "system", "content": "You are the Session Manager."},
            {"role": "user", "content": "Investigate the current launch status."},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "brain_search",
                    "description": "Search permission-filtered company context.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert budget.fits
    assert budget.remaining_for_context > 0


def test_oversized_fixed_request_leaves_no_retrieval_budget():
    budget = context_budget_for_request(
        context_window=64_000,
        output_tokens=16_000,
        messages_without_company_context=[
            {"role": "system", "content": "x" * 250_000}
        ],
        configured_context_budget=200_000,
    )
    assert budget == 0
