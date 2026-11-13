"""Reading images (report screenshots, table captures, dashboards) with a vision model.

At upload time an image is transcribed into text so it can be searched and read like any
other document. Transcriptions are machine-generated: callers must label them as such, and
agents can look at the original with the view_image tool when a detail matters.
"""

import base64
import io
from dataclasses import dataclass
from typing import Protocol

# Keep images comfortably inside provider limits (per-image size and dimensions) while
# preserving enough resolution for dense tables.
MAX_LONG_EDGE = 2400
MAX_BYTES = 4_500_000

TRANSCRIBE_PROMPT = """\
You transcribe business images (report screenshots, spreadsheet captures, dashboards) into \
faithful text for analysts who will model data from them. Transcribe only what is visible; \
never infer, complete or correct values. Mark anything you can't read as [unreadable].

Output Markdown with these sections, skipping any that don't apply:
## Overview
One or two sentences: what kind of image this is (table capture, dashboard, chart, form) and \
its title.
## Tables
Each table as a Markdown table, cell by cell, with the exact headers, row labels, numbers and \
units as shown (keep thousands separators and percent signs). Include totals rows.
## Charts and widgets
For each chart, KPI tile or widget: its title, type (bar, line, pie, KPI, map…), the measures \
and dimensions it shows, axis labels and units, legend/series names, visible data labels or \
values, and any filters or time range shown on it.
## Filters and controls
Dashboard-level filters, selectors and their current values.
## Notes and text
Footnotes, captions, definitions and any other text, verbatim.
"""


class VisionError(Exception):
    pass


@dataclass(frozen=True)
class PreparedImage:
    media_type: str
    data_b64: str


def prepare_image(data: bytes) -> PreparedImage:
    """Downscale very large images and re-encode so they fit provider limits."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise VisionError(f"not a readable image: {exc}") from exc
    if max(img.size) > MAX_LONG_EDGE:
        img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE))
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    media_type = "image/png"
    if buf.tell() > MAX_BYTES:  # photos and gradients compress poorly as PNG
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=88)
        media_type = "image/jpeg"
    if buf.tell() > MAX_BYTES:
        raise VisionError("image is too large to send even after resizing")
    return PreparedImage(media_type, base64.b64encode(buf.getvalue()).decode())


class ImageDescriber(Protocol):
    model: str

    def describe(self, data: bytes, filename: str) -> str: ...


class AnthropicImageDescriber:
    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int = 16000) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def describe(self, data: bytes, filename: str) -> str:
        image = prepare_image(data)
        a = self._anthropic
        try:
            # Streamed so a long transcription can't hit an HTTP timeout.
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=TRANSCRIBE_PROMPT,
                output_config={"effort": self.effort},
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.data_b64}},
                        {"type": "text", "text": f"Transcribe this image. File name: {filename}"},
                    ],
                }],
            ) as stream:
                message = stream.get_final_message()
        except a.APIStatusError as exc:
            raise VisionError(f"vision model error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise VisionError("could not reach the vision model") from exc
        if message.stop_reason == "refusal":
            raise VisionError("the vision model declined to transcribe this image")
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if not text:
            raise VisionError("the vision model returned no text")
        if message.stop_reason == "max_tokens":
            text += "\n\n[Transcription truncated: the image holds more than one pass can transcribe.]"
        return text


@dataclass
class FakeImageDescriber:
    text: str = "## Overview\nA test image.\n## Tables\n| Month | Gross sales |\n|---|---|\n| Jul 2026 | 132,000 |"
    model: str = "fake-vision"

    def describe(self, data: bytes, filename: str) -> str:
        prepare_image(data)  # still validates that it's a real image
        return self.text


def describer_from_settings(settings) -> ImageDescriber | None:
    provider = settings.image_description_provider
    if provider == "none":
        return None
    if provider == "fake":
        return FakeImageDescriber()
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            return None  # images are stored but not read until a key is configured
        return AnthropicImageDescriber(
            settings.anthropic_api_key, settings.image_description_model, settings.image_description_effort
        )
    raise ValueError(f"unknown image description provider {provider!r}")
