"""NeMo Data Designer integration: Nemotron writes the text parts of a demo case.

The numeric tables from ``variator.synthetic.numeric`` are used as seed data; Data
Designer renders one prompt per seed row and Nemotron 3.5 Lightning, served by
Nebius Token Factory, writes manager commentary, customer feedback snippets or
3PL vendor quote emails.

Provider policy: the only provider ever registered is Token Factory's chat
completions endpoint (``Settings.tf_base_url``). The library's built-in
third-party providers are never passed in, and ``run_designer`` refuses configs
whose models point at any other provider; the hackathon requires Nemotron calls
to go through Token Factory.

The API key is referenced by env var name (``NEBIUS_API_KEY``) and resolved at run
time from ``Settings`` or the environment, so it never appears in a serialized config.

``data_designer`` and ``pandas`` (the ``synthetic`` extra) are imported lazily, so
importing this module is cheap and the numeric generator works without them.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from variator.config import MissingCredentialsError, Settings
from variator.llm.client import split_reasoning
from variator.llm.models import ModelRegistry
from variator.synthetic.models import CellValue, DemoCase, DemoTable

if TYPE_CHECKING:
    from data_designer.config import DataDesignerConfigBuilder, ModelConfig, ModelProvider
    from data_designer.interface.data_designer import DataDesigner

TOKEN_FACTORY_PROVIDER = "nebius-token-factory"
LIGHTNING_ALIAS = "nemotron-lightning"
API_KEY_ENV = "NEBIUS_API_KEY"
DEFAULT_ARTIFACT_PATH = Path(".cache") / "data-designer"

# ASSUMPTION: Token Factory honours vLLM-style chat_template_kwargs for Nemotron 3.5
# Lightning (docs/research §4 marks this as unverified). If it does not, replies may
# contain inline <think> blocks, which run_designer strips with split_reasoning.
LIGHTNING_EXTRA_BODY: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}


class TextKind(StrEnum):
    COMMENTARY = "manager_commentary"
    FEEDBACK = "customer_feedback"
    VENDOR_EMAIL = "vendor_quote_email"


class DesignerResult(BaseModel):
    """Rows produced by one Data Designer run (seed columns + generated columns)."""

    records: list[dict[str, Any]]
    text_columns: list[str]
    model_ids: list[str]
    preview: bool
    artifact_path: str | None = None


class TokenFactoryOnlyError(ValueError):
    """Raised when a config would send requests to a provider other than Token Factory."""


# --- lazy imports -----------------------------------------------------------------


def _dd() -> Any:
    """Import ``data_designer.config`` with NeMo telemetry off unless the user opted in.

    Data Designer reads ``NEMO_TELEMETRY_ENABLED`` once at import time and otherwise
    posts usage events to an NVIDIA endpoint; we keep all traffic on Token Factory.
    """
    os.environ.setdefault("NEMO_TELEMETRY_ENABLED", "false")
    try:
        import data_designer.config as dd
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise ImportError("Data Designer is not installed; install the 'synthetic' extra") from exc
    return dd


class _SettingsSecretResolver:
    """Resolve the Token Factory key from ``Settings`` first, then the environment.

    Unlike Data Designer's default resolver there is no plaintext fallback, so a
    missing key fails loudly instead of being sent as the literal string "NEBIUS_API_KEY".
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, secret: str) -> str:
        if secret == API_KEY_ENV and self._settings.nebius_api_key:
            return self._settings.nebius_api_key
        value = os.environ.get(secret, "").strip()
        if value:
            return value
        from data_designer.engine.errors import SecretResolutionError

        raise SecretResolutionError(f"{secret} is not set")


# --- provider and model -----------------------------------------------------------


def token_factory_provider(settings: Settings | None = None) -> ModelProvider:
    """The single Data Designer model provider we register: Token Factory.

    ``provider_type`` keeps the library default, its chat completions API format.
    """
    settings = settings or Settings.from_env()
    return _dd().ModelProvider(
        name=TOKEN_FACTORY_PROVIDER,
        endpoint=settings.tf_base_url,
        api_key=API_KEY_ENV,  # env var name, resolved at run time
    )


def lightning_model_config(
    settings: Settings | None = None,
    *,
    alias: str = LIGHTNING_ALIAS,
    temperature: float = 0.8,
    top_p: float = 0.95,
    max_tokens: int = 400,
    max_parallel_requests: int = 4,
    timeout_s: int = 120,
) -> ModelConfig:
    """Nemotron 3.5 Lightning on Token Factory, reasoning off (short creative text)."""
    settings = settings or Settings.from_env()
    dd = _dd()
    return dd.ModelConfig(
        alias=alias,
        model=ModelRegistry(settings.model_overrides).model_id("lightning"),
        provider=TOKEN_FACTORY_PROVIDER,
        inference_parameters=dd.ChatCompletionInferenceParams(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            # Token Factory's base rate limit is ~60 RPM; keep fan-out modest.
            max_parallel_requests=max_parallel_requests,
            timeout=timeout_s,
            extra_body=LIGHTNING_EXTRA_BODY,
        ),
    )


# --- seed rows (pure Python, testable without Data Designer) ----------------------


def _signed_pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:+.{digits}f}%"


def _pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def _month_events(case: DemoCase) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {}
    if case.has_table("price_changes"):
        for row in case.table("price_changes").rows:
            scope = "all categories" if row["category"] == "all" else f"the {row['category']} category"
            events.setdefault(str(row["effective_month"]), []).append(
                f"list prices raised {_pct(float(row['price_change_pct']), 0)} on {scope}"
            )
    if case.has_table("warehouse_capacity"):
        previous_capacity: CellValue | None = None
        for row in case.table("warehouse_capacity").rows:
            month = str(row["month"])
            if previous_capacity is not None and row["capacity_orders"] != previous_capacity:
                events.setdefault(month, []).append(
                    f"warehouse capacity raised to {int(row['capacity_orders']):,} orders/month"
                )
            if int(row["overflow_orders_3pl"]) > 0:
                events.setdefault(month, []).append(
                    f"{int(row['overflow_orders_3pl']):,} orders overflowed to a spot 3PL"
                )
            previous_capacity = row["capacity_orders"]
    return events


def commentary_seed_rows(case: DemoCase, *, last_n_months: int | None = 12) -> list[dict[str, CellValue]]:
    """One row per month from ``monthly_pnl`` with derived growth and event notes."""
    pnl = case.table("monthly_pnl").rows
    events = _month_events(case)
    rows: list[dict[str, CellValue]] = []
    for i, row in enumerate(pnl):
        orders = int(row["orders"])
        mom = _signed_pct(orders / int(pnl[i - 1]["orders"]) - 1) if i >= 1 else "n/a"
        yoy = _signed_pct(orders / int(pnl[i - 12]["orders"]) - 1) if i >= 12 else "n/a"
        rows.append(
            {
                "company": case.company,
                "month": str(row["month"]),
                "orders": f"{orders:,}",
                "orders_mom": mom,
                "orders_yoy": yoy,
                "revenue": f"{float(row['revenue']):,.0f}",
                "margin_per_order": f"{float(row['margin_per_order']):.2f}",
                "inhouse_cost_per_order": f"{float(row['inhouse_cost_per_order']):.2f}",
                "marketing_spend": f"{float(row['marketing_spend']):,.0f}",
                "churn_rate": _pct(float(row["churn_rate"]), 2),
                "warehouse_utilization": _pct(float(row["warehouse_utilization"]), 0),
                "events": "; ".join(events.get(str(row["month"]), [])) or "none",
            }
        )
    return rows[-last_n_months:] if last_n_months else rows


def feedback_seed_rows(case: DemoCase) -> list[dict[str, CellValue]]:
    """One row per month and category with price and delivery context."""
    orders_rows = case.table("monthly_orders").rows
    pnl = {str(row["month"]): row for row in case.table("monthly_pnl").rows}
    on_time: dict[str, float] = {}
    if case.has_table("warehouse_capacity"):
        on_time = {
            str(r["month"]): float(r["on_time_ship_rate"]) for r in case.table("warehouse_capacity").rows
        }

    totals: dict[tuple[str, str], dict[str, float]] = {}
    for row in orders_rows:
        key = (str(row["month"]), str(row["category"]))
        bucket = totals.setdefault(key, {"orders": 0.0, "revenue": 0.0, "price_index": 0.0})
        bucket["orders"] += float(row["orders"])
        bucket["revenue"] += float(row["revenue"])
        bucket["price_index"] = float(row["list_price_index"])

    months = list(pnl)
    rows: list[dict[str, CellValue]] = []
    for (month, category), bucket in totals.items():
        position = months.index(month)
        earlier = totals.get((months[position - 2], category)) if position >= 2 else None
        change = bucket["price_index"] / earlier["price_index"] - 1 if earlier else 0.0
        price_note = (
            f"prices on {category} items went up about {_pct(change, 0)} in the last two months"
            if change > 0.005
            else "no recent price change"
        )
        utilization = float(pnl[month]["warehouse_utilization"])
        if month in on_time:
            delivery_note = f"{_pct(on_time[month], 1)} of orders shipped on time"
        elif utilization >= 0.99:
            delivery_note = "warehouse at full capacity, some orders shipped late"
        else:
            delivery_note = "deliveries mostly on time"
        rows.append(
            {
                "company": case.company,
                "month": month,
                "category": category,
                "orders": int(bucket["orders"]),
                "avg_order_value": f"{bucket['revenue'] / bucket['orders']:.2f}",
                "price_note": price_note,
                "delivery_note": delivery_note,
            }
        )
    return rows


def vendor_email_seed_rows(case: DemoCase) -> list[dict[str, CellValue]]:
    """One row per 3PL quote; only cases with a ``tpl_quotes`` table have them."""
    if not case.has_table("tpl_quotes"):
        raise ValueError(f"case {case.name!r} has no tpl_quotes table; vendor emails need the warehouse case")
    annual_orders = case.key_facts.get("base_orders") or case.key_facts.get("orders_last_12m") or 0.0
    rows: list[dict[str, CellValue]] = []
    for row in case.table("tpl_quotes").rows:
        rows.append(
            {
                "company": case.company,
                "annual_orders": f"{annual_orders:,.0f}",
                "vendor": str(row["vendor"]),
                "quote_date": str(row["quote_date"]),
                "region_coverage": str(row["region_coverage"]),
                "fee_per_order": f"{float(row['fee_per_order']):.2f}",
                "setup_fee": f"{int(row['setup_fee']):,}",
                "minimum_monthly_orders": f"{int(row['minimum_monthly_orders']):,}",
                "contract_months": int(row["contract_months"]),
                "annual_escalator": _pct(float(row["annual_escalator_pct"]), 1),
                "peak_surcharge_per_order": f"{float(row['peak_surcharge_per_order']):.2f}",
                "on_time_sla": _pct(float(row["on_time_sla_pct"]), 1),
            }
        )
    return rows


# --- prompts ----------------------------------------------------------------------

_COMMENTARY_SYSTEM = (
    "You are the operations and finance manager at {{ company }}, a mid-size online distributor of home, "
    "garden, kitchen and pet products. You write concise, factual internal monthly commentary for the "
    "leadership team. Use only the numbers you are given and do not invent new figures."
)
_COMMENTARY_PROMPT = """Write the monthly business commentary for {{ month }} in 3-4 sentences.

Data for the month:
- Orders: {{ orders }} (month on month {{ orders_mom }}, year on year {{ orders_yoy }})
- Revenue: ${{ revenue }}
- Contribution margin before fulfilment: ${{ margin_per_order }} per order
- In-house fulfilment cost: ${{ inhouse_cost_per_order }} per order
- Marketing spend: ${{ marketing_spend }}
- Monthly customer churn: {{ churn_rate }}
- Warehouse utilization: {{ warehouse_utilization }}
- Notable events: {{ events }}

Name the most important driver and one risk to watch. Plain text, no headings, no bullet points."""

_FEEDBACK_SYSTEM = (
    "You write realistic, short customer feedback for {{ company }}, an online retailer of home, garden, "
    "kitchen and pet products. Vary tone, length and wording like real customers do."
)
_FEEDBACK_PROMPT = (
    "Write one piece of customer feedback (1-3 sentences) received via {{ channel }} in {{ month }} "
    "about a {{ category }} order.\n"
    "Sentiment: {{ sentiment }}.\n"
    "Context you may reflect, without quoting numbers verbatim: typical order value about "
    "${{ avg_order_value }}; {{ price_note }}; {{ delivery_note }}.\n"
    "Return only the feedback text."
)

_VENDOR_SYSTEM = (
    "You are a sales manager at a third-party logistics (3PL) provider writing to a prospective client. "
    "Write professional, concise business emails."
)
_VENDOR_PROMPT = """Write a quote email from {{ vendor }} to the COO of {{ company }}, which ships about \
{{ annual_orders }} orders a year and needs fulfilment capacity for growth.

Quote terms (use these exact numbers):
- Fee per order (pick, pack, carrier handover): ${{ fee_per_order }}
- One-off setup fee: ${{ setup_fee }}
- Minimum volume: {{ minimum_monthly_orders }} orders per month
- Contract length: {{ contract_months }} months
- Annual price escalator: {{ annual_escalator }}
- Peak-season surcharge (Nov-Dec): ${{ peak_surcharge_per_order }} per order
- On-time shipping SLA: {{ on_time_sla }}
- Coverage: {{ region_coverage }}

Date the email {{ quote_date }} and start with a subject line. Under 180 words."""

FEEDBACK_SENTIMENTS = ("positive", "neutral", "negative")
FEEDBACK_SENTIMENT_WEIGHTS = (0.5, 0.3, 0.2)
FEEDBACK_CHANNELS = ("a product review", "a support email", "a post-purchase survey", "social media")


# --- config builders --------------------------------------------------------------


def _new_builder(
    rows: list[dict[str, CellValue]],
    *,
    settings: Settings | None,
    model_config: ModelConfig | None,
    shuffle: bool,
) -> DataDesignerConfigBuilder:
    dd = _dd()
    import pandas as pd

    if not rows:
        raise ValueError("no seed rows")
    model = model_config or lightning_model_config(settings)
    builder = dd.DataDesignerConfigBuilder(model_configs=[model])
    strategy = dd.SamplingStrategy.SHUFFLE if shuffle else dd.SamplingStrategy.ORDERED
    builder.with_seed_dataset(dd.DataFrameSeedSource(df=pd.DataFrame(rows)), sampling_strategy=strategy)
    return builder


def _text_column(name: str, *, system: str, prompt: str, model_alias: str) -> Any:
    return _dd().LLMTextColumnConfig(name=name, system_prompt=system, prompt=prompt, model_alias=model_alias)


def build_commentary_config(
    case: DemoCase,
    *,
    settings: Settings | None = None,
    model_config: ModelConfig | None = None,
    last_n_months: int | None = 12,
) -> DataDesignerConfigBuilder:
    """Manager monthly commentary, one record per month of ``monthly_pnl`` (chronological)."""
    model = model_config or lightning_model_config(settings)
    builder = _new_builder(
        commentary_seed_rows(case, last_n_months=last_n_months),
        settings=settings,
        model_config=model,
        shuffle=False,
    )
    builder.add_column(
        _text_column(
            TextKind.COMMENTARY.value,
            system=_COMMENTARY_SYSTEM,
            prompt=_COMMENTARY_PROMPT,
            model_alias=model.alias,
        )
    )
    return builder


def build_feedback_config(
    case: DemoCase,
    *,
    settings: Settings | None = None,
    model_config: ModelConfig | None = None,
) -> DataDesignerConfigBuilder:
    """Customer feedback snippets seeded by month x category, with sampled sentiment and channel."""
    dd = _dd()
    model = model_config or lightning_model_config(settings)
    builder = _new_builder(feedback_seed_rows(case), settings=settings, model_config=model, shuffle=True)
    builder.add_column(
        dd.SamplerColumnConfig(
            name="sentiment",
            sampler_type=dd.SamplerType.CATEGORY,
            params=dd.CategorySamplerParams(
                values=list(FEEDBACK_SENTIMENTS), weights=list(FEEDBACK_SENTIMENT_WEIGHTS)
            ),
        )
    )
    builder.add_column(
        dd.SamplerColumnConfig(
            name="channel",
            sampler_type=dd.SamplerType.CATEGORY,
            params=dd.CategorySamplerParams(values=list(FEEDBACK_CHANNELS)),
        )
    )
    builder.add_column(
        _text_column(
            TextKind.FEEDBACK.value, system=_FEEDBACK_SYSTEM, prompt=_FEEDBACK_PROMPT, model_alias=model.alias
        )
    )
    return builder


def build_vendor_email_config(
    case: DemoCase,
    *,
    settings: Settings | None = None,
    model_config: ModelConfig | None = None,
) -> DataDesignerConfigBuilder:
    """3PL vendor quote emails, one per row of ``tpl_quotes`` (warehouse case)."""
    model = model_config or lightning_model_config(settings)
    builder = _new_builder(vendor_email_seed_rows(case), settings=settings, model_config=model, shuffle=False)
    builder.add_column(
        _text_column(
            TextKind.VENDOR_EMAIL.value, system=_VENDOR_SYSTEM, prompt=_VENDOR_PROMPT, model_alias=model.alias
        )
    )
    return builder


def available_text_kinds(case: DemoCase) -> list[TextKind]:
    """Text kinds that have the seed tables they need in this case."""
    kinds = [TextKind.COMMENTARY, TextKind.FEEDBACK]
    if case.has_table("tpl_quotes"):
        kinds.append(TextKind.VENDOR_EMAIL)
    return kinds


def build_text_config(
    case: DemoCase,
    kind: TextKind | str,
    *,
    settings: Settings | None = None,
    model_config: ModelConfig | None = None,
) -> DataDesignerConfigBuilder:
    """Dispatch to the builder for ``kind``."""
    builders = {
        TextKind.COMMENTARY: build_commentary_config,
        TextKind.FEEDBACK: build_feedback_config,
        TextKind.VENDOR_EMAIL: build_vendor_email_config,
    }
    return builders[TextKind(kind)](case, settings=settings, model_config=model_config)


# --- running ----------------------------------------------------------------------


def make_data_designer(
    settings: Settings | None = None,
    *,
    artifact_path: str | Path | None = None,
    auto_configure_logging: bool = False,
) -> DataDesigner:
    """A ``DataDesigner`` whose only provider is Token Factory.

    Artifacts default to ``.cache/data-designer`` (gitignored) because Data Designer
    otherwise creates ``./artifacts`` even for in-memory previews.

    Note: Data Designer's constructor seeds default provider YAML files in
    ``$DATA_DESIGNER_HOME`` (``~/.data-designer``) as a side effect; they are not used
    because we pass ``model_providers`` explicitly.
    """
    settings = settings or Settings.from_env()
    _dd()
    from data_designer.interface.data_designer import DataDesigner

    return DataDesigner(
        artifact_path=artifact_path or DEFAULT_ARTIFACT_PATH,
        model_providers=[token_factory_provider(settings)],
        secret_resolver=_SettingsSecretResolver(settings),
        auto_configure_logging=auto_configure_logging,
    )


def _check_token_factory_only(
    config: DataDesignerConfigBuilder, designer: DataDesigner, settings: Settings
) -> None:
    expected = settings.tf_base_url.rstrip("/")
    for provider in designer.model_provider_registry.providers:
        if provider.endpoint.rstrip("/") != expected:
            raise TokenFactoryOnlyError(
                f"provider {provider.name!r} points at {provider.endpoint}; "
                f"only Token Factory ({expected}) is allowed"
            )
    for model in config.model_configs:
        if model.provider != TOKEN_FACTORY_PROVIDER:
            raise TokenFactoryOnlyError(f"model alias {model.alias!r} uses provider {model.provider!r}")


def _native(value: Any) -> Any:
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def run_designer(
    config: DataDesignerConfigBuilder,
    *,
    num_records: int,
    preview: bool = True,
    settings: Settings | None = None,
    artifact_path: str | Path | None = None,
    designer: DataDesigner | None = None,
    dataset_name: str = "variator-demo-text",
) -> DesignerResult:
    """Generate ``num_records`` rows through Token Factory.

    ``preview=True`` keeps results in memory (``DataDesigner.preview``); otherwise
    ``DataDesigner.create`` writes parquet artifacts under ``artifact_path``.
    Raises ``MissingCredentialsError`` before any network call if no key is set.
    """
    settings = settings or Settings.from_env()
    settings.require("nebius_api_key")
    designer = designer or make_data_designer(settings, artifact_path=artifact_path)
    _check_token_factory_only(config, designer, settings)

    artifact: str | None = None
    if preview:
        dataset = designer.preview(config, num_records=num_records).dataset
    else:
        results = designer.create(config, num_records=num_records, dataset_name=dataset_name)
        dataset = results.load_dataset()
        artifact = str(results.artifact_storage.base_dataset_path)
    if dataset is None:  # pragma: no cover - Data Designer raises before this
        raise RuntimeError("Data Designer returned no dataset")

    dd = _dd()
    text_columns = [c.name for c in config.get_column_configs() if isinstance(c, dd.LLMTextColumnConfig)]
    records: list[dict[str, Any]] = []
    for raw in dataset.to_dict(orient="records"):
        record = {str(key): _native(value) for key, value in raw.items()}
        for column in text_columns:
            if isinstance(record.get(column), str):
                record[column] = split_reasoning(record[column])[0].strip()
        records.append(record)
    return DesignerResult(
        records=records,
        text_columns=text_columns,
        model_ids=[model.model for model in config.model_configs],
        preview=preview,
        artifact_path=artifact,
    )


# --- turning generated text into demo tables --------------------------------------

_TABLE_COLUMNS: dict[TextKind, list[str]] = {
    TextKind.COMMENTARY: ["month", TextKind.COMMENTARY.value],
    TextKind.FEEDBACK: ["month", "category", "channel", "sentiment", TextKind.FEEDBACK.value],
    TextKind.VENDOR_EMAIL: ["vendor", "quote_date", TextKind.VENDOR_EMAIL.value],
}

_TABLE_DESCRIPTIONS: dict[TextKind, str] = {
    TextKind.COMMENTARY: "Manager monthly commentary (synthetic, written by Nemotron from monthly_pnl).",
    TextKind.FEEDBACK: "Customer feedback snippets (synthetic, written by Nemotron from monthly_orders).",
    TextKind.VENDOR_EMAIL: "3PL quote emails (synthetic, written by Nemotron from tpl_quotes).",
}


def text_records_table(result: DesignerResult, kind: TextKind | str) -> DemoTable:
    """Keep the identifying seed columns plus the generated text as a ``DemoTable``."""
    kind = TextKind(kind)
    columns = _TABLE_COLUMNS[kind]
    rows: list[dict[str, CellValue]] = []
    for record in result.records:
        rows.append({column: "" if record.get(column) is None else str(record[column]) for column in columns})
    return DemoTable(name=kind.value, description=_TABLE_DESCRIPTIONS[kind], columns=columns, rows=rows)


def _seed_size(case: DemoCase, kind: TextKind) -> int:
    if kind == TextKind.COMMENTARY:
        return len(commentary_seed_rows(case))
    if kind == TextKind.VENDOR_EMAIL:
        return len(vendor_email_seed_rows(case))
    return 24  # feedback: a shuffled sample is enough for a demo


def add_text_tables(
    case: DemoCase,
    *,
    kinds: list[TextKind] | None = None,
    num_records: int | None = None,
    preview: bool = True,
    settings: Settings | None = None,
    designer: DataDesigner | None = None,
) -> DemoCase:
    """Return a copy of ``case`` with generated text tables added (or replaced).

    Makes real Token Factory calls; raises ``MissingCredentialsError`` without a key.
    """
    settings = settings or Settings.from_env()
    if not settings.nebius_api_key:
        raise MissingCredentialsError("NEBIUS_API_KEY is not set (see .env.example)")
    designer = designer or make_data_designer(settings)
    tables = {table.name: table for table in case.tables}
    for kind in kinds or available_text_kinds(case):
        config = build_text_config(case, kind, settings=settings)
        result = run_designer(
            config,
            num_records=num_records or _seed_size(case, kind),
            preview=preview,
            settings=settings,
            designer=designer,
        )
        table = text_records_table(result, kind)
        tables[table.name] = table
    return case.model_copy(update={"tables": list(tables.values())})
