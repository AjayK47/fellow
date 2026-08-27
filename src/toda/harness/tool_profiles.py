"""Named tool visibility profiles.

The registry contains the complete capability catalog; a run receives only
the profile appropriate to its role and destination.  Profiles are sets of
names, not permission grants: policy, authorization and approval still run
for every selected tool.
"""

from __future__ import annotations

CONVERSATION = frozenset(
    {
        "memory_search",
        "memory_read_source",
        "memory_query_facts",
        "case_list",
        "judgment_guidance",
        "owner_resolve",
        "procedure_find",
        "artifact_list",
        "artifact_read",
        "skill_list",
        "skill_read",
        "task_search",
        "task_get",
        "session_search",
        "integration_list",
        "automation_list",
        "web_search",
        "web_fetch",
        "calculate",
    }
)

READ_ONLY_SPECIALIST = frozenset(
    {
        "list_files",
        "read_file",
        "search_files",
        "file_diff",
        "memory_search",
        "memory_read_source",
        "memory_query_facts",
        "case_list",
        "judgment_guidance",
        "owner_resolve",
        "procedure_find",
        "artifact_list",
        "artifact_read",
        "web_search",
        "web_fetch",
        "calculate",
        "skill_list",
        "skill_read",
        "integration_list",
        "integration_search",
        "integration_describe",
    }
)


PROFILES = {
    "conversation": CONVERSATION,
    "read_only_specialist": READ_ONLY_SPECIALIST,
}


def resolve(profile: object) -> frozenset[str] | None:
    if isinstance(profile, str):
        # An explicitly supplied but unknown profile is a configuration
        # failure, not permission to fall back to the complete registry.
        return PROFILES.get(profile, frozenset())
    if isinstance(profile, (list, tuple, set, frozenset)):
        return frozenset(str(item) for item in profile)
    return None
