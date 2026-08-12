from __future__ import annotations

import argparse

from toda import main


def test_wait_for_gateway_retries_until_healthy(monkeypatch):
    attempts = iter([OSError("starting"), object()])

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def open_gateway(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return Response()

    monkeypatch.setattr(main.urllib.request, "urlopen", open_gateway)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    main._wait_for_gateway(timeout_seconds=1)


def test_session_secret_is_created_once(tmp_path):
    env_path = tmp_path / ".env"

    main._ensure_session_secret(env_path)
    first = env_path.read_text()
    main._ensure_session_secret(env_path)

    assert env_path.read_text() == first
    assert first.count("TODA_DASHBOARD_SESSION_SECRET=") == 1


def test_macos_restart_kicks_loaded_service_without_unloading(monkeypatch, tmp_path, capsys):
    calls: list[list[str]] = []
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(main, "_wait_for_gateway", lambda: None)
    monkeypatch.setattr(main, "_toda_executable", lambda: "/bin/toda")
    service_path = tmp_path / "Library" / "LaunchAgents" / "ai.toda.gateway.plist"
    service_path.parent.mkdir(parents=True)
    service_path.write_text("installed")

    def command(arguments):
        calls.append(arguments)
        return main.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(main, "_command", command)
    main.service_cli(argparse.Namespace(action="restart", no_open=True))

    assert any(call[:3] == ["launchctl", "kickstart", "-k"] for call in calls)
    assert not any(call[:2] == ["launchctl", "bootout"] for call in calls)
    assert capsys.readouterr().out.startswith("Toda restarted.")


def test_macos_install_restarts_loaded_service_in_place(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(main, "_wait_for_gateway", lambda: None)
    monkeypatch.setattr(main, "_toda_executable", lambda: "/bin/toda")
    monkeypatch.setattr(main.SetupBootstrapToken, "issue", lambda _self: "token")

    def command(arguments):
        calls.append(arguments)
        return main.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(main, "_command", command)
    main.service_cli(argparse.Namespace(action="install", no_open=True))

    assert any(call[:3] == ["launchctl", "kickstart", "-k"] for call in calls)
    assert not any(call[:2] == ["launchctl", "bootout"] for call in calls)


def test_status_is_short_and_human_readable(tmp_path, capsys):
    main._print_status(
        running=True,
        installed=True,
        data_dir=tmp_path,
        log_path=tmp_path / "toda.log",
    )

    output = capsys.readouterr().out
    assert output.startswith("Toda is running.")
    assert "Starts at login: yes" in output
    assert "launchctl" not in output
