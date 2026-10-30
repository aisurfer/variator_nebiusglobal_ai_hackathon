"""Data Designer integration, fully offline: config building, provider policy, mocked runs."""

from pathlib import Path

import pytest

pytest.importorskip("data_designer")

from data_designer.config import LLMTextColumnConfig, SamplerColumnConfig  # noqa: E402
from data_designer.engine.errors import SecretResolutionError  # noqa: E402

from variator.config import DEFAULT_TF_BASE_URL, MissingCredentialsError, Settings  # noqa: E402
from variator.llm import LIGHTNING  # noqa: E402
from variator.synthetic import (  # noqa: E402
    API_KEY_ENV,
    TOKEN_FACTORY_PROVIDER,
    TextKind,
    TokenFactoryOnlyError,
    add_text_tables,
    available_text_kinds,
    build_commentary_config,
    build_text_config,
    build_vendor_email_config,
    commentary_seed_rows,
    feedback_seed_rows,
    generate_demo_case,
    lightning_model_config,
    load_demo_case,
    make_data_designer,
    run_designer,
    text_records_table,
    token_factory_provider,
    vendor_email_seed_rows,
    write_demo_case,
)
from variator.synthetic.designer import _SettingsSecretResolver  # noqa: E402

THIRD_PARTY_HOSTS = ("integrate.api.nvidia.com", "api.example-llm.com", "openrouter.ai")
KEYED = Settings(nebius_api_key="test-key")


@pytest.fixture(scope="module")
def warehouse():
    return generate_demo_case("warehouse")


@pytest.fixture(scope="module")
def pricing():
    return generate_demo_case("price_increase")


# --- seed rows ---------------------------------------------------------------------


def test_commentary_seed_rows_cover_last_months_with_events(warehouse, pricing):
    rows = commentary_seed_rows(warehouse)
    assert [row["month"] for row in rows][0] == "2025-11" and rows[-1]["month"] == "2026-10"
    by_month = {row["month"]: row for row in commentary_seed_rows(warehouse, last_n_months=None)}
    assert len(by_month) == 36
    assert by_month["2023-11"]["orders_yoy"] == "n/a"
    assert "overflowed to a spot 3PL" in by_month["2026-05"]["events"]
    assert "capacity raised" in by_month["2024-07"]["events"]
    price_events = {row["month"]: row["events"] for row in commentary_seed_rows(pricing, last_n_months=None)}
    assert "all categories" in price_events["2025-10"]


def test_feedback_seed_rows_reflect_price_changes(pricing):
    rows = feedback_seed_rows(pricing)
    assert len(rows) == 36 * 4
    note = {(r["month"], r["category"]): r["price_note"] for r in rows}
    assert "went up" in note[("2024-04", "pet")]
    assert note[("2024-04", "home")] == "no recent price change"


def test_vendor_email_rows_need_quotes(warehouse, pricing):
    rows = vendor_email_seed_rows(warehouse)
    assert [r["vendor"] for r in rows] == warehouse.table("tpl_quotes").column("vendor")
    with pytest.raises(ValueError, match="tpl_quotes"):
        vendor_email_seed_rows(pricing)
    assert TextKind.VENDOR_EMAIL in available_text_kinds(warehouse)
    assert TextKind.VENDOR_EMAIL not in available_text_kinds(pricing)


# --- provider and configs ------------------------------------------------------------


def test_provider_points_at_token_factory_with_env_key_reference():
    import data_designer.config as dd

    provider = token_factory_provider(Settings())
    assert provider.name == TOKEN_FACTORY_PROVIDER
    assert provider.endpoint == DEFAULT_TF_BASE_URL
    # The library default: its chat completions API format.
    assert provider.provider_type == dd.ModelProvider.model_fields["provider_type"].default
    assert provider.api_key == API_KEY_ENV == "NEBIUS_API_KEY"
    custom = token_factory_provider(Settings(tf_base_url="https://tf.example/v1/"))
    assert custom.endpoint == "https://tf.example/v1/"


def test_key_never_lands_in_the_provider_config():
    provider = token_factory_provider(Settings(nebius_api_key="secret-value"))
    assert "secret-value" not in provider.model_dump_json()


def test_lightning_model_config():
    model = lightning_model_config(Settings())
    assert model.model == LIGHTNING.model_id == "nvidia/Nemotron-3_5-Lightning"
    assert model.provider == TOKEN_FACTORY_PROVIDER
    assert model.inference_parameters.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    overridden = lightning_model_config(Settings(model_overrides={"lightning": "nvidia/lightning-v2"}))
    assert overridden.model == "nvidia/lightning-v2"


@pytest.mark.parametrize("kind", list(TextKind))
def test_text_configs_build_offline(warehouse, kind):
    builder = build_text_config(warehouse, kind, settings=Settings())
    config = builder.build()
    assert [m.provider for m in config.model_configs] == [TOKEN_FACTORY_PROVIDER]
    assert [m.model for m in config.model_configs] == [LIGHTNING.model_id]
    llm_columns = [c for c in builder.get_column_configs() if isinstance(c, LLMTextColumnConfig)]
    assert [c.name for c in llm_columns] == [kind.value]
    seed_columns = set(builder.get_seed_config().source.df.columns)
    sampler_columns = {c.name for c in builder.get_column_configs() if isinstance(c, SamplerColumnConfig)}
    for column in llm_columns:
        assert set(column.required_columns) <= seed_columns | sampler_columns


def test_configs_validate_against_seed_data(warehouse, dd_offline, tmp_path):
    designer = make_data_designer(KEYED, artifact_path=tmp_path)
    for kind in TextKind:
        designer.validate(build_text_config(warehouse, kind, settings=KEYED))


def test_only_token_factory_provider_is_registered(dd_offline, tmp_path):
    designer = make_data_designer(KEYED, artifact_path=tmp_path)
    providers = designer.model_provider_registry.providers
    assert [p.name for p in providers] == [TOKEN_FACTORY_PROVIDER]
    assert providers[0].endpoint == DEFAULT_TF_BASE_URL
    for provider in providers:
        assert not any(host in provider.endpoint for host in THIRD_PARTY_HOSTS)


def test_secret_resolver_has_no_plaintext_fallback(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(SecretResolutionError):
        _SettingsSecretResolver(Settings()).resolve(API_KEY_ENV)
    assert _SettingsSecretResolver(KEYED).resolve(API_KEY_ENV) == "test-key"
    monkeypatch.setenv(API_KEY_ENV, "from-env")
    assert _SettingsSecretResolver(Settings()).resolve(API_KEY_ENV) == "from-env"


def test_run_designer_without_key_raises(warehouse, monkeypatch, no_network):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    config = build_commentary_config(warehouse, settings=Settings())
    with pytest.raises(MissingCredentialsError, match="NEBIUS_API_KEY"):
        run_designer(config, num_records=2, settings=Settings())
    with pytest.raises(MissingCredentialsError):
        run_designer(config, num_records=2)  # reads Settings.from_env()
    with pytest.raises(MissingCredentialsError):
        add_text_tables(warehouse, settings=Settings())


def test_run_designer_refuses_other_providers(warehouse, dd_offline, tmp_path):
    import data_designer.config as dd

    foreign = dd.ModelConfig(alias="other", model="example-model-1", provider="example-llm")
    config = build_commentary_config(warehouse, settings=KEYED, model_config=foreign)
    with pytest.raises(TokenFactoryOnlyError):
        run_designer(config, num_records=1, settings=KEYED, artifact_path=tmp_path)

    rogue = make_data_designer(Settings(nebius_api_key="k", tf_base_url="https://api.example-llm.com/v1"))
    with pytest.raises(TokenFactoryOnlyError):
        config = build_commentary_config(warehouse, settings=KEYED)
        run_designer(config, num_records=1, settings=KEYED, designer=rogue)


# --- mocked end-to-end runs ------------------------------------------------------------


def test_preview_goes_through_token_factory(warehouse, fake_tf_chat, tmp_path):
    fake_tf_chat.reply = "<think>scratch work</think>Orders rose on garden demand; watch capacity."
    config = build_commentary_config(warehouse, settings=KEYED, last_n_months=3)
    result = run_designer(config, num_records=3, settings=KEYED, artifact_path=tmp_path)

    assert result.preview and result.model_ids == [LIGHTNING.model_id]
    assert [r["month"] for r in result.records] == ["2026-08", "2026-09", "2026-10"]
    expected_text = "Orders rose on garden demand; watch capacity."
    assert all(r["manager_commentary"] == expected_text for r in result.records)

    # one health-check probe plus one call per record, all to Token Factory with the key
    assert len(fake_tf_chat.requests) == 4
    assert all(url == DEFAULT_TF_BASE_URL + "chat/completions" for url in fake_tf_chat.urls)
    assert set(fake_tf_chat.auth_headers) == {"Bearer test-key"}
    assert {body["model"] for body in fake_tf_chat.requests} == {LIGHTNING.model_id}
    assert all(body["chat_template_kwargs"] == {"enable_thinking": False} for body in fake_tf_chat.requests)
    prompts = [p for p in fake_tf_chat.user_prompts if "monthly business commentary" in p]
    assert len(prompts) == 3
    october = next(p for p in prompts if "2026-10" in p)
    expected_orders = f"{warehouse.table('monthly_pnl').rows[-1]['orders']:,}"
    assert f"Orders: {expected_orders}" in october


def test_create_writes_artifacts_and_text_table(warehouse, fake_tf_chat, tmp_path):
    fake_tf_chat.reply = "Subject: Fulfilment quote\n\nDear COO, ..."
    config = build_vendor_email_config(warehouse, settings=KEYED)
    result = run_designer(config, num_records=4, preview=False, settings=KEYED, artifact_path=tmp_path)
    assert not result.preview
    assert result.artifact_path and Path(result.artifact_path).exists()
    assert {r["vendor"] for r in result.records} == set(warehouse.table("tpl_quotes").column("vendor"))
    table = text_records_table(result, TextKind.VENDOR_EMAIL)
    assert table.columns == ["vendor", "quote_date", "vendor_quote_email"]
    assert table.rows[0]["vendor_quote_email"].startswith("Subject:")


def test_add_text_tables_round_trips_through_csv(warehouse, fake_tf_chat, tmp_path):
    designer = make_data_designer(KEYED, artifact_path=tmp_path / "artifacts")
    enriched = add_text_tables(
        warehouse,
        kinds=[TextKind.COMMENTARY, TextKind.FEEDBACK],
        num_records=2,
        settings=KEYED,
        designer=designer,
    )
    assert enriched.table_names[-2:] == ["manager_commentary", "customer_feedback"]
    feedback = enriched.table("customer_feedback")
    assert {row["sentiment"] for row in feedback.rows} <= {"positive", "neutral", "negative"}
    write_demo_case(enriched, tmp_path / "case")
    assert load_demo_case(tmp_path / "case") == enriched
    assert warehouse.table_names == enriched.table_names[:-2]  # original case untouched
