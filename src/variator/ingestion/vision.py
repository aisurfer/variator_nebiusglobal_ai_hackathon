"""Read charts, dashboards and scanned report pages with Nemotron 3.5 Super VL.

``read_chart`` returns a structured ``ChartReading``; ``facts_from_image`` turns
its takeaways into facts (origin DOCUMENT) grounded in the numbers read from
the image. Images are validated (type, magic bytes, size) before any call.
"""

from __future__ import annotations

import logging
import math

from pydantic import BaseModel, Field

from variator.domain import Fact, FactOrigin
from variator.ingestion import prompts
from variator.ingestion.facts import dedupe_facts
from variator.ingestion.grounding import (
    DEFAULT_REL_TOL,
    NumberIndex,
    UnverifiedPolicy,
    ground_facts,
    source_numbers,
)
from variator.llm import TokenFactoryClient, image_part, user_message

log = logging.getLogger(__name__)

# ASSUMPTION: Super VL accepts PNG, JPEG and WebP data URLs. The docs show image_url
# parts but list no formats (https://docs.tokenfactory.nebius.com/api-reference/examples/vision-capabilities.md).
SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
# The OpenAPI schema caps ImageURL.url at 13,981,514 characters
# (https://api.tokenfactory.nebius.com/openapi.json), i.e. ~10 MiB of raw bytes in base64.
MAX_DATA_URL_CHARS = 13_981_514
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_FACT_MAX_CONFIDENCE = 0.6  # values read off a chart are approximate

_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}


class ImageValidationError(ValueError):
    """The image cannot be sent to the vision model (type, content or size)."""


def sniff_image_type(data: bytes) -> str | None:
    """MIME type from magic bytes, or None if unrecognized."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return None


def data_url_length(size: int, mime: str) -> int:
    return len(f"data:{mime};base64,") + 4 * math.ceil(size / 3)


def validate_image(image: bytes, mime: str, *, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> str:
    """Check type, content and size; return the normalized MIME type.

    Raises ``ImageValidationError`` with a message fit for the end user.
    """
    if not image:
        raise ImageValidationError("image is empty")
    normalized = _MIME_ALIASES.get(mime.split(";")[0].strip().lower(), mime.split(";")[0].strip().lower())
    supported = ", ".join(sorted(SUPPORTED_IMAGE_TYPES))
    if normalized not in SUPPORTED_IMAGE_TYPES:
        hint = " (render PDF pages to PNG first)" if normalized == "application/pdf" else ""
        raise ImageValidationError(f"unsupported image type {mime!r}; use {supported}{hint}")
    if len(image) > max_bytes:
        raise ImageValidationError(
            f"image is {len(image) / 1e6:.1f} MB; the limit is {max_bytes / 1e6:.1f} MB (downscale it)"
        )
    if data_url_length(len(image), normalized) > MAX_DATA_URL_CHARS:
        raise ImageValidationError("image is too large for the Token Factory request limit (~10 MiB)")
    actual = sniff_image_type(image)
    if actual is None:
        raise ImageValidationError(f"data does not look like an image ({supported})")
    if actual != normalized:
        raise ImageValidationError(f"declared {normalized} but the bytes are {actual}")
    return normalized


class Axis(BaseModel):
    label: str | None = None
    unit: str | None = None


class ChartPoint(BaseModel):
    x: str = Field(description='Category or x label as printed, e.g. "2025-03", "Q1", "North"')
    y: float | None = Field(default=None, description="Value; null if unreadable")


class ChartSeries(BaseModel):
    name: str
    unit: str | None = None
    points: list[ChartPoint] = Field(default_factory=list)


class ChartTakeaway(BaseModel):
    statement: str = Field(description="One self-contained sentence")
    value: float | None = Field(default=None, description="Key number, exactly as read from the image")
    unit: str | None = None
    period: str | None = None


class ChartReading(BaseModel):
    """Structured content of a chart, table or report page image."""

    is_chart: bool = Field(description="False if the image has no chart, table or figures")
    chart_type: str | None = Field(default=None, description="line, bar, pie, table, kpi_tiles...")
    title: str | None = None
    x_axis: Axis = Field(default_factory=Axis)
    y_axis: Axis = Field(default_factory=Axis)
    series: list[ChartSeries] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list, description="Printed numbers and labels, verbatim")
    takeaways: list[ChartTakeaway] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    def grounding_numbers(self) -> list[float]:
        """Numbers on the image plus simple derivations a takeaway may state
        (changes, growth, shares of total, min/max/sum/mean per series)."""
        numbers: list[float] = []
        texts = [self.title or "", self.x_axis.label or "", self.y_axis.label or "", *self.annotations]
        for text in texts:
            numbers += source_numbers(text)
        for series in self.series:
            ys = [p.y for p in series.points if p.y is not None]
            numbers += ys
            for p in series.points:
                numbers += source_numbers(p.x)
            if not ys:
                continue
            total = sum(ys)
            numbers += [min(ys), max(ys), total, total / len(ys), max(ys) - min(ys)]
            ratios: list[float] = []
            if total:
                ratios += [y / total for y in ys]
            pairs = [(ys[0], ys[-1])] + list(zip(ys, ys[1:], strict=False))
            for a, b in pairs:
                numbers.append(b - a)
                if a:
                    ratios += [b / a - 1, b / a]
            numbers += ratios + [r * 100 for r in ratios]
        return numbers


def read_chart(
    client: TokenFactoryClient,
    image: bytes,
    *,
    mime: str,
    source: str,
    model: str = "super_vl",
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    reasoning: bool = False,
    max_tokens: int | None = None,
) -> ChartReading:
    """Read a chart or report page image into a ``ChartReading`` (one Super VL call).

    Raises ``ImageValidationError`` before any network call when the image is
    unsupported or too large, and ``StructuredOutputError`` if the model never
    returns valid JSON.
    """
    mime = validate_image(image, mime, max_bytes=max_bytes)
    content = (
        prompts.CHART_READING_INSTRUCTIONS
        + "\n\n"
        + prompts.CHART_READING_TEMPLATE.format(source=source)
    )
    message = user_message(content, images=[image_part(image, mime)])
    return client.structured(model, [message], ChartReading, reasoning=reasoning, max_tokens=max_tokens)


def facts_from_reading(
    reading: ChartReading,
    *,
    source: str,
    on_unverified: UnverifiedPolicy = "drop",
    rel_tol: float = DEFAULT_REL_TOL,
) -> list[Fact]:
    """Turn chart takeaways into facts (origin DOCUMENT) without another model call.

    Takeaway values must match a number read from the image or a simple derivation
    of the series (see ``ChartReading.grounding_numbers``).
    """
    if not reading.is_chart:
        return []
    facts = [
        Fact(
            statement=t.statement.strip(),
            value=t.value,
            unit=t.unit or (reading.y_axis.unit if t.value is not None else None),
            period=t.period,
            origin=FactOrigin.DOCUMENT,
            source=source,
            confidence=IMAGE_FACT_MAX_CONFIDENCE,
        )
        for t in reading.takeaways
        if t.statement.strip()
    ]
    report = ground_facts(facts, NumberIndex(reading.grounding_numbers()), policy=on_unverified,
                          rel_tol=rel_tol)
    if report.rejected:
        log.warning("%s: dropped %d takeaway(s) with numbers not on the image", source, len(report.rejected))
    return dedupe_facts(report.kept)


def facts_from_image(
    client: TokenFactoryClient,
    image: bytes,
    *,
    mime: str,
    source: str,
    model: str = "super_vl",
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    on_unverified: UnverifiedPolicy = "drop",
    rel_tol: float = DEFAULT_REL_TOL,
    reasoning: bool = False,
) -> list[Fact]:
    """Read an image with Super VL and return its grounded takeaways as facts.

    Use ``read_chart`` + ``facts_from_reading`` instead when you also need the
    reading itself (e.g. to redraw the chart), to avoid a second call.
    """
    reading = read_chart(client, image, mime=mime, source=source, model=model, max_bytes=max_bytes,
                         reasoning=reasoning)
    return facts_from_reading(reading, source=source, on_unverified=on_unverified, rel_tol=rel_tol)
