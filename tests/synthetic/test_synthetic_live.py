"""Real Data Designer preview through Token Factory. Skipped unless ``pytest --live`` with NEBIUS_API_KEY."""

import pytest

pytest.importorskip("data_designer")

from variator.config import Settings  # noqa: E402
from variator.synthetic import (  # noqa: E402
    build_commentary_config,
    build_vendor_email_config,
    generate_demo_case,
    run_designer,
)

pytestmark = pytest.mark.live


def test_live_tiny_commentary_preview(tmp_path):
    """Verifies the Lightning id, the TF endpoint and enable_thinking=False end to end."""
    settings = Settings.from_env()
    case = generate_demo_case("warehouse")
    config = build_commentary_config(case, settings=settings, last_n_months=2)
    result = run_designer(config, num_records=2, settings=settings, artifact_path=tmp_path)
    assert len(result.records) == 2
    for record in result.records:
        text = record["manager_commentary"]
        assert isinstance(text, str) and len(text.split()) >= 15
        assert "<think>" not in text


def test_live_vendor_email_mentions_the_quoted_fee(tmp_path):
    settings = Settings.from_env()
    case = generate_demo_case("warehouse")
    config = build_vendor_email_config(case, settings=settings)
    result = run_designer(config, num_records=1, settings=settings, artifact_path=tmp_path)
    record = result.records[0]
    assert record["fee_per_order"] in record["vendor_quote_email"]
