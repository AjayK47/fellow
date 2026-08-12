---
name: audio-media
display_name: Audio Transcription and Speech
description: Transcribe voice/audio artifacts or create spoken-audio artifacts when the user sends voice media, requests a transcript, or asks for text to be spoken.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: builtin
platform_key: builtin.audio_media
activation: {mode: optional}
required_capabilities: {any_of: [media.transcribe, media.tts]}
steps:
  - {order: 1, name: Inspect input, description: Use scoped artifacts and preserve the requested language, title, and voice constraints.}
  - {order: 2, name: Transform, description: Call media_transcribe or text_to_speech through the stable media tool.}
  - {order: 3, name: Verify, description: Confirm the transcript is nonempty or the generated audio artifact exists before delivery.}
---

Slack voice messages may already arrive with a transcript and source artifact
ID. Reuse those instead of transcribing the same media again.

- Use `media_transcribe` for a scoped audio artifact. Cite both the source audio artifact and the transcript artifact.
- Use `text_to_speech` for spoken output. The configured backend owns the provider, model, default voice, and format.
- Do not expose credentials or ask the user to choose a vendor unless they are configuring the deployment.
- If the requested capability reports `setup_needed`, explain that transcription or speech generation is not connected and repeat the exact missing-capability names.
- A successful HTTP response is not sufficient evidence; deliver only the verified scoped artifact returned by the tool.
