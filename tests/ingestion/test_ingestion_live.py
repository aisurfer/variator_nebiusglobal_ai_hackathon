"""Smoke tests against the real Token Factory API. Run with ``pytest --live``."""

import io

import pytest

from variator.config import Settings
from variator.ingestion import (
    facts_from_image,
    facts_from_profile,
    facts_from_text,
    parse_question,
    profile_csv,
    read_chart,
)
from variator.llm import TokenFactoryClient

pytestmark = pytest.mark.live

def _row(i: int) -> str:
    revenue = 100_000 * 1.01**i * (1.2 if i % 12 == 11 else 1)
    return f"{2023 + i // 12}-{i % 12 + 1:02d}-01,{revenue:.0f},{900 + i}"


CSV = "\n".join(["month,revenue,orders"] + [_row(i) for i in range(36)])


@pytest.fixture(scope="module")
def client():
    return TokenFactoryClient(Settings.from_env())


def bar_chart_png() -> bytes:
    """A labelled bar chart drawn with Pillow (installed via data-designer)."""
    pil = pytest.importorskip("PIL.Image")
    draw_mod = pytest.importorskip("PIL.ImageDraw")
    image = pil.new("RGB", (640, 400), "white")
    draw = draw_mod.Draw(image)
    draw.text((200, 10), "Revenue by year (EUR million)", fill="black")
    for i, (year, value) in enumerate([("2023", 3.2), ("2024", 3.7), ("2025", 4.2)]):
        x = 120 + i * 160
        top = 360 - int(value * 70)
        draw.rectangle([x, top, x + 80, 360], fill="steelblue")
        draw.text((x + 25, top - 15), f"{value}", fill="black")
        draw.text((x + 25, 370), year, fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_live_facts_from_profile(client):
    facts = facts_from_profile(client, profile_csv(CSV, name="sales.csv"), max_facts=10)
    assert facts and all(f.source.startswith("sales.csv") for f in facts)


def test_live_facts_from_text(client):
    text = "ACME GmbH reported revenue of EUR 4.2 million in 2025, up 12.5% on 2024. Gross margin was 38.4%."
    facts = facts_from_text(client, text, source="report.txt")
    assert any(f.value is not None and abs(f.value - 4.2) < 1e-6 for f in facts) or facts


def test_live_read_chart(client):
    reading = read_chart(client, bar_chart_png(), mime="image/png", source="chart.png")
    assert reading.is_chart and reading.series


def test_live_facts_from_image(client):
    facts = facts_from_image(client, bar_chart_png(), mime="image/png", source="chart.png")
    assert isinstance(facts, list)


def test_live_parse_question(client):
    brief = parse_question(client, "Should we raise prices by 8% next quarter or keep them flat?")
    assert len(brief.options) >= 2 and brief.horizon_months >= 1
