from __future__ import annotations

from toda.services.gateway_setup import GatewaySetupService, SetupBootstrapToken


def test_bootstrap_token_is_one_time_and_digest_only(tmp_path):
    bootstrap = SetupBootstrapToken(tmp_path, ttl_seconds=60)
    token = bootstrap.issue()

    contents = bootstrap.path.read_text()
    assert token not in contents
    assert bootstrap.consume(token) is True
    assert bootstrap.consume(token) is False


def test_expired_bootstrap_token_is_rejected(tmp_path):
    bootstrap = SetupBootstrapToken(tmp_path, ttl_seconds=-1)
    token = bootstrap.issue()

    assert bootstrap.consume(token) is False


def test_setup_status_is_resumable_from_persisted_configuration(empty_brain, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("TODA_ENV_FILE", str(env_file))
    service = GatewaySetupService(
        empty_brain.store,
        empty_brain.graph,
        empty_brain.audit,
        skills=empty_brain.skills,
    )

    initial = service.status()
    assert initial["setup"]["current_step"] == "provider"

    service.configure_provider(
        connection_id="company",
        provider="openrouter",
        api_key="test-key",
        assignments={"main": "openai/gpt-5"},
    )

    resumed = service.status()
    assert resumed["setup"]["current_step"] == "slack"
    assert next(step for step in resumed["setup"]["steps"] if step["id"] == "provider")["status"] == "complete"

    service.configure_provider(
        connection_id="company",
        provider="openrouter",
        api_key="",
        assignments={"embedding": "qwen/qwen3-embedding-8b"},
    )
    env_contents = env_file.read_text()
    assert "test-key" in env_contents
    assert "qwen/qwen3-embedding-8b" in env_contents


def test_setup_status_never_returns_slack_bot_token(empty_brain, tmp_path, monkeypatch):
    monkeypatch.setenv("TODA_ENV_FILE", str(tmp_path / ".env"))
    empty_brain.store.put_organization({"id": "org_safe", "name": "Safe Co"})
    empty_brain.store.upsert_slack_installation(
        {
            "id": "slack:org_safe:T_SAFE",
            "organization_id": "org_safe",
            "workspace_id": "T_SAFE",
            "workspace_name": "Safe Co",
            "bot_user_id": "U_BOT",
            "bot_token": "must-never-leave-backend",
        }
    )
    service = GatewaySetupService(
        empty_brain.store, empty_brain.graph, empty_brain.audit
    )

    encoded = str(service.status())

    assert "must-never-leave-backend" not in encoded
    assert service.status()["slack"]["installations"][0]["workspace_id"] == "T_SAFE"
