---
name: image-creation
display_name: Image Creation and Editing
description: Generate a new image or edit scoped reference images when the user asks for artwork, mockups, illustrations, or image variations.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: builtin
platform_key: builtin.image_creation
activation: {mode: optional}
required_capabilities: {all_of: [image.generate]}
steps:
  - {order: 1, name: Define outcome, description: Turn the request into a concrete visual brief without inventing brand requirements.}
  - {order: 2, name: Generate, description: Call image_generate once with the brief and any scoped reference artifact IDs.}
  - {order: 3, name: Verify, description: Inspect the returned artifact and revise only if it does not satisfy the request.}
---

Use `image_generate` as the only image backend surface. Never select or name a
provider or model; deployment configuration owns both.

- Preserve the user's subject, composition, style, colors, text, and aspect-ratio constraints in the prompt.
- Pass existing images through `reference_artifact_ids`; never use host paths or external URLs.
- Treat the returned artifact as provisional until it has been visually inspected with `vision_analyze` when that tool is ready.
- If the tool reports `setup_needed`, state that image generation is not connected and name only the returned missing capabilities.
- Deliver the exact artifact ID or download URL returned by the tool. Do not invent a URL or claim success from the provider call alone.
