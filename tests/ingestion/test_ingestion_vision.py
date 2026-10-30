import base64

import pytest

from variator.domain import FactOrigin
from variator.ingestion import (
    ChartReading,
    ImageValidationError,
    facts_from_image,
    facts_from_reading,
    read_chart,
    validate_image,
)
from variator.ingestion.vision import IMAGE_FACT_MAX_CONFIDENCE, MAX_DATA_URL_CHARS, data_url_length
from variator.llm import SUPER_VL, StructuredOutputError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64

READING = {
    "is_chart": True,
    "chart_type": "bar",
    "title": "Revenue by year, EUR m",
    "x_axis": {"label": "Year", "unit": None},
    "y_axis": {"label": "Revenue", "unit": "million EUR"},
    "series": [
        {
            "name": "Revenue",
            "unit": "million EUR",
            "points": [{"x": "2023", "y": 3.2}, {"x": "2024", "y": 3.7}, {"x": "2025", "y": 4.2}],
        }
    ],
    "annotations": ["CAGR 14.6%", "4.2"],
    "takeaways": [
        {"statement": "Revenue reached EUR 4.2 million in 2025.", "value": 4.2, "unit": "million EUR",
         "period": "2025"},
        {"statement": "Revenue grew 31% from 2023 to 2025.", "value": 31.25, "unit": "%"},
        {"statement": "Revenue CAGR was 14.6%.", "value": 14.6, "unit": "%"},
        {"statement": "Revenue will hit EUR 6 million in 2026.", "value": 6.0},  # not on the image
        {"statement": "Growth is steady.", "value": None},
    ],
    "caveats": [],
}


@pytest.mark.parametrize(
    ("data", "mime", "expected"),
    [(PNG, "image/png", "image/png"), (JPEG, "image/jpg", "image/jpeg"), (JPEG, "IMAGE/JPEG", "image/jpeg"),
     (WEBP, "image/webp; q=1", "image/webp")],
)
def test_validate_image_accepts_supported(data, mime, expected):
    assert validate_image(data, mime) == expected


@pytest.mark.parametrize(
    ("data", "mime", "message"),
    [
        (b"", "image/png", "empty"),
        (b"GIF89a" + b"\x00" * 10, "image/gif", "unsupported image type"),
        (b"%PDF-1.7", "application/pdf", "render PDF pages"),
        (PNG, "image/jpeg", "declared image/jpeg but the bytes are image/png"),
        (b"hello world", "image/png", "does not look like an image"),
    ],
)
def test_validate_image_rejects(data, mime, message):
    with pytest.raises(ImageValidationError, match=message):
        validate_image(data, mime)


def test_validate_image_size_limits():
    with pytest.raises(ImageValidationError, match="limit is"):
        validate_image(PNG + b"\x00" * 1000, "image/png", max_bytes=500)
    huge = PNG + b"\x00" * (11 * 1024 * 1024)
    with pytest.raises(ImageValidationError, match="request limit"):
        validate_image(huge, "image/png", max_bytes=50 * 1024 * 1024)


def test_default_limit_fits_the_api_data_url_cap():
    assert data_url_length(10 * 1024 * 1024, "image/webp") <= MAX_DATA_URL_CHARS


def test_read_chart_request_shape(fake_tf):
    fake_tf.add_json_reply(READING)
    reading = read_chart(fake_tf.client(), PNG, mime="image/png", source="deck.pdf p.4")
    assert reading.series[0].points[-1].y == 4.2
    request = fake_tf.requests[0]
    assert request["model"] == SUPER_VL.model_id
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["name"] == "ChartReading"
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    content = request["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("You read business charts")
    assert content[0]["text"].endswith("Source: deck.pdf p.4")
    url = content[1]["image_url"]["url"]
    assert content[1]["type"] == "image_url" and url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


def test_read_chart_validates_before_calling(fake_tf):
    with pytest.raises(ImageValidationError):
        read_chart(fake_tf.client(), PNG, mime="image/png", source="x", max_bytes=10)
    assert fake_tf.requests == []


def test_read_chart_structured_output_error(fake_tf):
    fake_tf.add_reply(content="I see a chart").add_reply(content="Really, a chart")
    with pytest.raises(StructuredOutputError):
        read_chart(fake_tf.client(), PNG, mime="image/png", source="x")


def test_facts_from_image_grounds_takeaways(fake_tf):
    fake_tf.add_json_reply(READING)
    facts = facts_from_image(fake_tf.client(), JPEG, mime="image/jpeg", source="deck.pdf p.4")
    assert [f.value for f in facts] == [4.2, 31.25, 14.6, None]
    assert all(f.origin == FactOrigin.DOCUMENT and f.source == "deck.pdf p.4" for f in facts)
    assert all(f.confidence == IMAGE_FACT_MAX_CONFIDENCE for f in facts)  # none downgraded
    assert facts[0].unit == "million EUR"
    assert len(fake_tf.requests) == 1


def test_facts_from_reading_downgrade_and_not_a_chart():
    reading = ChartReading.model_validate(READING)
    facts = facts_from_reading(reading, source="s", on_unverified="downgrade")
    assert len(facts) == 5 and facts[3].confidence == 0.3
    assert facts_from_reading(ChartReading(is_chart=False), source="s") == []


def test_chart_grounding_numbers_include_derivations():
    numbers = ChartReading.model_validate(READING).grounding_numbers()
    for expected in (3.2, 4.2, 11.1, 1.0, 0.3125, 31.25, 14.6, 0.146):
        assert any(abs(n - expected) < 1e-9 for n in numbers), expected
