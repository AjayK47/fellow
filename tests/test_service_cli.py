from __future__ import annotations

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
