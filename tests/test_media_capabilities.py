from __future__ import annotations

import base64
import io

import httpx
from PIL import Image

from toda.config import Settings
from toda.harness.media_ingestion import SlackMediaIngestion
from toda.harness.media_providers import GeneratedMedia, build_image_provider, build_speech_provider, build_transcription_provider, image_providers
from toda.harness.model_client import DeterministicHarnessModel, ScriptedModelClient, build_vision_model
from toda.harness.models import EventActor, EventScope, ModelResponse, NormalizedEvent
from toda.harness.typed_tools import VisionMediaTools


class FakeImageProvider:
    name = "fake-image"
    model = "image-test-v1"

    def generate(self, *, prompt, aspect_ratio, references):
        image = Image.new("RGB", (12, 8), "#336699")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return GeneratedMedia(output.getvalue(), "image/png", self.name, self.model)


class FakeTranscriptionProvider:
    name = "fake-transcription"
    model = "stt-test-v1"

    def transcribe(self, **kwargs):
        assert kwargs["payload"]
        return "Please send the revised launch plan."


class FakeSpeechProvider:
    name = "fake-speech"
    model = "tts-test-v1"

    def synthesize(self, *, text, voice=None):
        return GeneratedMedia(b"ID3" + b"\0" * 100, "audio/mpeg", self.name, self.model)


def _runtime(brain, tool_name: str) -> dict:
    return {
        "organization_id": "org_acme",
        "task_id": "task_media_test",
        "tool_name": tool_name,
        "envelope": {
            "principal_id": "person_ajay",
            "destination": {"type": "dm", "id": "D_MEDIA", "is_private": True},
            "allowed_scope_ids": ["scope_org_public"],
        },
    }


def test_media_backends_are_independently_configured():
    config = Settings(
        provider_connections={
            "image": {"provider": "openai", "api_key": "image-key"},
            "speech": {"provider": "openai", "api_key": "speech-key"},
        },
        model_assignments={
            "image": {"connection": "image", "model": "image-model"},
            "tts": {"connection": "speech", "model": "speech-model"},
        },
    )
    assert build_image_provider(config).model == "image-model"
    assert build_transcription_provider(config) is None
    assert build_speech_provider(config).model == "speech-model"


def test_openrouter_media_backends_reuse_main_key_and_use_native_contracts(monkeypatch):
    requests: list[dict] = []

    def fake_post(url, **kwargs):
        requests.append({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        if url.endswith("/images"):
            encoded = base64.b64encode(b"generated image").decode("ascii")
            return httpx.Response(
                200, json={"data": [{"b64_json": encoded, "media_type": "image/webp"}]},
                request=request,
            )
        if url.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "  Transcribed.  "}, request=request)
        return httpx.Response(200, content=b"ID3 audio", request=request)

    monkeypatch.setattr("toda.harness.media_providers.httpx.post", fake_post)
    config = Settings(
        provider_connections={
            "openrouter-company": {"provider": "openrouter", "api_key": "shared-openrouter-key"}
        },
        model_assignments={
            "image": {"connection": "openrouter-company", "model": "google/gemini-3.1-flash-lite-image"},
            "transcription": {"connection": "openrouter-company", "model": "x-ai/grok-stt-1.0"},
            "tts": {"connection": "openrouter-company", "model": "fish-audio/s2.1-pro-free:free"},
        },
        tts_voice="default",
    )

    image = build_image_provider(config).generate(
        prompt="Restyle this", aspect_ratio="16:9", references=[(b"source", "image/png")]
    )
    transcript = build_transcription_provider(config).transcribe(
        payload=b"audio bytes", filename="voice.mp3", mime_type="audio/mpeg", language="en"
    )
    speech = build_speech_provider(config).synthesize(text="Hello")

    assert image.mime_type == "image/webp"
    assert transcript == "Transcribed."
    assert speech.payload == b"ID3 audio"
    assert all(request["headers"]["Authorization"] == "Bearer shared-openrouter-key" for request in requests)

    image_request, transcription_request, speech_request = requests
    assert image_request["url"] == "https://openrouter.ai/api/v1/images"
    assert image_request["json"]["aspect_ratio"] == "16:9"
    assert image_request["json"]["input_references"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert transcription_request["json"]["input_audio"] == {
        "data": base64.b64encode(b"audio bytes").decode("ascii"), "format": "mp3"
    }
    assert transcription_request["json"]["language"] == "en"
    assert speech_request["json"]["model"] == "fish-audio/s2.1-pro-free:free"
    assert speech_request["json"]["voice"] == "default"


def test_image_backend_can_be_registered_without_changing_the_tool():
    image_providers.register("custom", lambda config: FakeImageProvider())
    provider = build_image_provider(Settings(
        provider_connections={"local": {"provider": "custom", "base_url": "http://localhost:11434/v1"}},
        model_assignments={"image": {"connection": "local", "model": "local-image"}},
    ))
    assert provider is not None
    assert provider.name == "fake-image"


def test_vision_prefers_main_only_when_declared_capable():
    main = DeterministicHarnessModel()
    assert build_vision_model(Settings(main_model_supports_vision=False), main) is None
    assert build_vision_model(Settings(main_model_supports_vision=True), main) is main
    auxiliary = build_vision_model(
        Settings(
            provider_connections={"vision": {"provider": "openai", "api_key": "vision-key"}},
            model_assignments={"vision": {"connection": "vision", "model": "vision-model"}},
        ), main
    )
    assert auxiliary is not None
    assert auxiliary.model == "vision-model"


def test_stable_media_tools_create_verified_scoped_artifacts(brain):
    tools = VisionMediaTools(
        brain.artifacts,
        image_provider=FakeImageProvider(),
        transcription_provider=FakeTranscriptionProvider(),
        speech_provider=FakeSpeechProvider(),
    )
    image_result = tools.execute({"prompt": "A blue tile", "aspect_ratio": "3:2"}, _runtime(brain, "image_generate"))
    image_artifact = brain.artifacts.get(image_result["artifact_id"])
    assert image_artifact["scope_id"] == "scope_org_public"
    assert image_artifact["metadata"]["provider"] == "fake-image"

    source = brain.artifacts.create(
        organization_id="org_acme", artifact_type="audio.upload", title="note.mp3",
        content={"base64": "SUQzAAAA", "mime_type": "audio/mpeg"},
        scope_id="scope_org_public", created_by="person_ajay",
    )
    transcript_result = tools.execute({"artifact_id": source["id"]}, _runtime(brain, "media_transcribe"))
    assert transcript_result["transcript"].startswith("Please send")
    assert brain.artifacts.get(transcript_result["artifact_id"])["metadata"]["source_artifact_id"] == source["id"]

    speech_result = tools.execute({"text": "Launch is ready."}, _runtime(brain, "text_to_speech"))
    assert brain.artifacts.get(speech_result["artifact_id"])["content"]["mime_type"] == "audio/mpeg"


def test_vision_tool_passes_pixels_to_configured_model(brain):
    source = brain.artifacts.create(
        organization_id="org_acme", artifact_type="image.upload", title="screen.png",
        content={"base64": "iVBORw0KGgo=", "mime_type": "image/png"},
        scope_id="scope_org_public", created_by="person_ajay",
    )
    model = ScriptedModelClient([ModelResponse(content="A settings panel is visible.", model="vision-test")])
    tools = VisionMediaTools(brain.artifacts, vision_model=model)
    result = tools.execute({"artifact_id": source["id"], "question": "What is shown?"}, _runtime(brain, "vision_analyze"))
    content = model.requests[0]["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result["observations"] == "A settings panel is visible."


def test_slack_voice_is_ingested_transcribed_and_attached_to_event(brain, monkeypatch):
    ingestion = SlackMediaIngestion(
        store=brain.store, artifacts=brain.artifacts, bot_token="xoxb-test",
        transcription_provider=FakeTranscriptionProvider(), max_bytes=1024,
    )
    monkeypatch.setattr(ingestion, "_download", lambda url, token: b"ID3 voice bytes")
    event = NormalizedEvent(
        organization_id="org_acme", source="slack", type="slack.dm",
        actor=EventActor(id="person_ajay", source_id="U1"),
        scope=EventScope(workspace_id="T1", channel_id="D1", visibility="organization_public", scope_id="scope_org_public"),
        payload={"text": "", "files": [{"id": "F1", "name": "voice.mp3", "mimetype": "audio/mpeg", "url_private_download": "https://files.slack.com/files-pri/T1-F1/voice.mp3"}]},
        dedupe_key="voice-test",
    )
    enriched = ingestion.enrich(event)
    assert "Voice message transcript" in enriched.payload["text"]
    assert enriched.payload["media_artifact_ids"]
    assert enriched.payload["transcript_artifact_ids"]
    assert brain.artifacts.get(enriched.payload["media_artifact_ids"][0])["scope_id"] == "scope_org_public"


def test_media_skills_are_discoverable_and_capability_gated(brain):
    assert brain.skills.package_catalog.get("image-creation").manifest.required_capabilities == {"all_of": ["image.generate"]}
    assert brain.skills.package_catalog.get("audio-media").manifest.required_capabilities == {"any_of": ["media.transcribe", "media.tts"]}
    browser = brain.skills.package_catalog.get("browser-interaction")
    assert "browser_snapshot" in browser.body
    names = {definition.name for definition in brain.tool_registry.definitions()}
    assert {"media_transcribe", "text_to_speech", "browser_get_images", "browser_vision", "browser_cdp"} <= names
