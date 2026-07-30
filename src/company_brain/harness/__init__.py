"""Company-wide agent harness.

This package owns the Slack/event activation plane and the durable Hermes-style
model/tool loop.  The existing ``services`` package remains the organizational
knowledge plane used by the harness.
"""

from company_brain.harness.models import (
    AgentRunState,
    AttentionDecision,
    InvocationEnvelope,
    NormalizedEvent,
)

__all__ = [
    "AgentRunState",
    "AttentionDecision",
    "InvocationEnvelope",
    "NormalizedEvent",
]
