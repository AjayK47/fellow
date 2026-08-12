"""Provider-neutral media contracts and OpenAI-compatible adapters.

The agent never selects a vendor or model.  Deployment configuration chooses
those, while tools expose one stable operation per capability.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable, Generic, Protocol, TypeVar

import httpx

from toda.config import Settings
from toda.providers import ProviderResolver, ResolvedModel


@dataclass(frozen=True)
class GeneratedMedia:
    payload: bytes
    mime_type: str
    provider: str
    model: str


class ImageGenerationProvider(Protocol):
    name: str
    model: str

    def generate(
        self, *, prompt: str, aspect_ratio: str, references: list[tuple[bytes, str]]
    ) -> GeneratedMedia: ...


class TranscriptionProvider(Protocol):
    name: str
    model: str

    def transcribe(
        self, *, payload: bytes, filename: str, mime_type: str, language: str | None = None
    ) -> str: ...


class SpeechProvider(Protocol):
    name: str
    model: str

    def synthesize(self, *, text: str, voice: str | None = None) -> GeneratedMedia: ...


ProviderT = TypeVar("ProviderT")


class MediaProviderRegistry(Generic[ProviderT]):
    """Small plugin surface: provider packages register a Settings factory."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[Settings], ProviderT]] = {}

    def register(self, name: str, factory: Callable[[Settings], ProviderT]) -> None:
        key = name.strip().lower()
        if not key:
            raise ValueError("media provider name cannot be empty")
        self._factories[key] = factory

    def build(self, name: str, config: Settings) -> ProviderT | None:
        factory = self._factories.get(name.strip().lower())
        return factory(config) if factory else None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


class _OpenAICompatible:
    def __init__(self, *, name: str, api_key: str, model: str, base_url: str, timeout: float) -> None:
        self.name = name
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _raise(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{self.name} media API returned {response.status_code}: {response.text[:2000]}"
            ) from exc


class OpenAICompatibleImageProvider(_OpenAICompatible):
    _SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}

    def generate(
        self, *, prompt: str, aspect_ratio: str, references: list[tuple[bytes, str]]
    ) -> GeneratedMedia:
        size = self._SIZES.get(aspect_ratio, "1024x1024")
        wants_gpt_image_defaults = self.model.startswith("gpt-image-")
        if references:
            files = [
                ("image[]", (f"reference-{index}.png", payload, mime))
                for index, (payload, mime) in enumerate(references, 1)
            ]
            response = httpx.post(
                f"{self.base_url}/images/edits",
                headers=self._headers,
                data={
                    "model": self.model, "prompt": prompt, "size": size,
                    **({} if wants_gpt_image_defaults else {"response_format": "b64_json"}),
                },
                files=files,
                timeout=self.timeout,
            )
        else:
            response = httpx.post(
                f"{self.base_url}/images/generations",
                headers={**self._headers, "Content-Type": "application/json"},
                json={
                    "model": self.model, "prompt": prompt, "size": size,
                    **({} if wants_gpt_image_defaults else {"response_format": "b64_json"}),
                },
                timeout=self.timeout,
            )
        self._raise(response)
        item = ((response.json().get("data") or [{}])[0])
        encoded = item.get("b64_json")
        if not isinstance(encoded, str):
            raise RuntimeError("image provider did not return base64 image data")
        return GeneratedMedia(base64.b64decode(encoded), "image/png", self.name, self.model)


class OpenRouterImageProvider(_OpenAICompatible):
    """Adapter for OpenRouter's dedicated image-generation endpoint."""

    def generate(
        self, *, prompt: str, aspect_ratio: str, references: list[tuple[bytes, str]]
    ) -> GeneratedMedia:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
        }
        if references:
            body["input_references"] = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"
                    },
                }
                for payload, mime_type in references
            ]
        response = httpx.post(
            f"{self.base_url}/images",
            headers={**self._headers, "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        self._raise(response)
        item = (response.json().get("data") or [{}])[0]
        encoded = item.get("b64_json")
        if not isinstance(encoded, str):
            raise RuntimeError("image provider did not return base64 image data")
        mime_type = item.get("media_type")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            mime_type = "image/png"
        return GeneratedMedia(base64.b64decode(encoded), mime_type, self.name, self.model)


class OpenAICompatibleTranscriptionProvider(_OpenAICompatible):
    def transcribe(
        self, *, payload: bytes, filename: str, mime_type: str, language: str | None = None
    ) -> str:
        data: dict[str, str] = {"model": self.model, "response_format": "json"}
        if language:
            data["language"] = language
        response = httpx.post(
            f"{self.base_url}/audio/transcriptions",
            headers=self._headers,
            data=data,
            files={"file": (filename, payload, mime_type)},
            timeout=self.timeout,
        )
        self._raise(response)
        text = response.json().get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("transcription provider returned no transcript")
        return text.strip()


class OpenRouterTranscriptionProvider(_OpenAICompatible):
    """Adapter for OpenRouter's JSON/base64 transcription contract."""

    _MIME_FORMATS = {
        "audio/aac": "aac",
        "audio/flac": "flac",
        "audio/m4a": "m4a",
        "audio/mp4": "mp4",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/opus": "opus",
        "audio/wav": "wav",
        "audio/webm": "webm",
        "audio/x-m4a": "m4a",
        "audio/x-wav": "wav",
    }

    def transcribe(
        self, *, payload: bytes, filename: str, mime_type: str, language: str | None = None
    ) -> str:
        audio_format = self._MIME_FORMATS.get(mime_type.lower())
        if not audio_format:
            suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            audio_format = suffix if suffix in set(self._MIME_FORMATS.values()) else "wav"
        body: dict[str, Any] = {
            "model": self.model,
            "input_audio": {
                "data": base64.b64encode(payload).decode("ascii"),
                "format": audio_format,
            },
        }
        if language:
            body["language"] = language
        response = httpx.post(
            f"{self.base_url}/audio/transcriptions",
            headers={**self._headers, "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        self._raise(response)
        text = response.json().get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("transcription provider returned no transcript")
        return text.strip()


class OpenAICompatibleSpeechProvider(_OpenAICompatible):
    _MIMES = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/opus", "aac": "audio/aac", "flac": "audio/flac"}

    def __init__(self, *, voice: str, output_format: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.voice = voice
        self.output_format = output_format

    def synthesize(self, *, text: str, voice: str | None = None) -> GeneratedMedia:
        body = {
            "model": self.model,
            "input": text,
            "response_format": self.output_format,
        }
        selected_voice = voice or self.voice
        if selected_voice:
            body["voice"] = selected_voice
        response = httpx.post(
            f"{self.base_url}/audio/speech",
            headers={**self._headers, "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        self._raise(response)
        if not response.content:
            raise RuntimeError("speech provider returned empty audio")
        return GeneratedMedia(
            response.content,
            self._MIMES.get(self.output_format, "application/octet-stream"),
            self.name,
            self.model,
        )


def _resolve(config: Settings, role: str) -> ResolvedModel:
    resolved = ProviderResolver(config.provider_connections, config.model_assignments).resolve(role)
    if not resolved:
        raise ValueError(f"model role {role!r} is not configured")
    return resolved


def _openai_image(config: Settings) -> ImageGenerationProvider:
    resolved = _resolve(config, "image")
    return OpenAICompatibleImageProvider(
        name=resolved.profile.name, api_key=resolved.connection.api_key or "local",
        model=resolved.assignment.model, base_url=resolved.profile.endpoint(resolved.connection),
        timeout=config.model_timeout_seconds,
    )


def _openai_transcription(config: Settings) -> TranscriptionProvider:
    resolved = _resolve(config, "transcription")
    return OpenAICompatibleTranscriptionProvider(
        name=resolved.profile.name, api_key=resolved.connection.api_key or "local",
        model=resolved.assignment.model, base_url=resolved.profile.endpoint(resolved.connection),
        timeout=config.model_timeout_seconds,
    )


def _openai_speech(config: Settings) -> SpeechProvider:
    resolved = _resolve(config, "tts")
    return OpenAICompatibleSpeechProvider(
        name=resolved.profile.name, api_key=resolved.connection.api_key or "local",
        model=resolved.assignment.model, base_url=resolved.profile.endpoint(resolved.connection),
        timeout=config.model_timeout_seconds,
        voice=config.tts_voice, output_format=config.tts_format,
    )


def _openrouter_image(config: Settings) -> ImageGenerationProvider:
    resolved = _resolve(config, "image")
    return OpenRouterImageProvider(
        name="openrouter", api_key=resolved.connection.api_key or "",
        model=resolved.assignment.model, base_url=resolved.profile.endpoint(resolved.connection),
        timeout=config.model_timeout_seconds,
    )


def _openrouter_transcription(config: Settings) -> TranscriptionProvider:
    resolved = _resolve(config, "transcription")
    return OpenRouterTranscriptionProvider(
        name="openrouter", api_key=resolved.connection.api_key or "",
        model=resolved.assignment.model, base_url=resolved.profile.endpoint(resolved.connection),
        timeout=config.model_timeout_seconds,
    )


def _openrouter_speech(config: Settings) -> SpeechProvider:
    resolved = _resolve(config, "tts")
    return OpenAICompatibleSpeechProvider(
        name="openrouter", api_key=resolved.connection.api_key or "",
        model=resolved.assignment.model, base_url=resolved.profile.endpoint(resolved.connection),
        timeout=config.model_timeout_seconds, voice=config.tts_voice,
        output_format=config.tts_format,
    )


image_providers: MediaProviderRegistry[ImageGenerationProvider] = MediaProviderRegistry()
transcription_providers: MediaProviderRegistry[TranscriptionProvider] = MediaProviderRegistry()
speech_providers: MediaProviderRegistry[SpeechProvider] = MediaProviderRegistry()
for _provider_name in ("openai", "openai-compatible"):
    image_providers.register(_provider_name, _openai_image)
    transcription_providers.register(_provider_name, _openai_transcription)
    speech_providers.register(_provider_name, _openai_speech)
image_providers.register("openrouter", _openrouter_image)
transcription_providers.register("openrouter", _openrouter_transcription)
speech_providers.register("openrouter", _openrouter_speech)


def build_image_provider(config: Settings) -> ImageGenerationProvider | None:
    resolved = ProviderResolver(config.provider_connections, config.model_assignments).resolve("image")
    if not resolved:
        return None
    try:
        return image_providers.build(resolved.profile.name, config) or _openai_image(config)
    except ValueError:
        return None


def build_transcription_provider(config: Settings) -> TranscriptionProvider | None:
    resolved = ProviderResolver(config.provider_connections, config.model_assignments).resolve("transcription")
    if not resolved:
        return None
    try:
        return transcription_providers.build(resolved.profile.name, config) or _openai_transcription(config)
    except ValueError:
        return None


def build_speech_provider(config: Settings) -> SpeechProvider | None:
    resolved = ProviderResolver(config.provider_connections, config.model_assignments).resolve("tts")
    if not resolved:
        return None
    try:
        return speech_providers.build(resolved.profile.name, config) or _openai_speech(config)
    except ValueError:
        return None
