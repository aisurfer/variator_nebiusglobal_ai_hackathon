"""Ingestion: turn company inputs into grounded facts and a decision brief.

* ``profile_csv``: deterministic table profile with trend statistics (no LLM).
* ``facts_from_profile`` / ``facts_from_text``: Nemotron 3.5 Lightning extraction,
  guarded so fact values must exist in the source.
* ``read_chart`` / ``facts_from_image``: Nemotron 3.5 Super VL chart reading.
* ``parse_question``: frame the leader's question as a ``DecisionBrief``.
"""

from variator.ingestion.facts import (
    DataFactDraft,
    DataFactList,
    FactDraft,
    FactList,
    chunk_text,
    dedupe_facts,
    facts_from_profile,
    facts_from_text,
)
from variator.ingestion.grounding import (
    GroundingReport,
    NumberIndex,
    ground_facts,
    numbers_in_text,
    source_numbers,
)
from variator.ingestion.question import (
    Constraint,
    ConstraintKind,
    DecisionBrief,
    DecisionBriefDraft,
    OptionCandidate,
    parse_question,
    to_identifier,
)
from variator.ingestion.tables import (
    CategoryCount,
    ColumnKind,
    ColumnProfile,
    NumericStats,
    TableProfile,
    decode_text,
    profile_csv,
)
from variator.ingestion.trends import PeriodValue, SeasonalIndex, SeriesTrend
from variator.ingestion.vision import (
    DEFAULT_MAX_IMAGE_BYTES,
    SUPPORTED_IMAGE_TYPES,
    Axis,
    ChartPoint,
    ChartReading,
    ChartSeries,
    ChartTakeaway,
    ImageValidationError,
    facts_from_image,
    facts_from_reading,
    read_chart,
    validate_image,
)

__all__ = [
    "DEFAULT_MAX_IMAGE_BYTES",
    "SUPPORTED_IMAGE_TYPES",
    "Axis",
    "CategoryCount",
    "ChartPoint",
    "ChartReading",
    "ChartSeries",
    "ChartTakeaway",
    "ColumnKind",
    "ColumnProfile",
    "Constraint",
    "ConstraintKind",
    "DataFactDraft",
    "DataFactList",
    "DecisionBrief",
    "DecisionBriefDraft",
    "FactDraft",
    "FactList",
    "GroundingReport",
    "ImageValidationError",
    "NumberIndex",
    "NumericStats",
    "OptionCandidate",
    "PeriodValue",
    "SeasonalIndex",
    "SeriesTrend",
    "TableProfile",
    "chunk_text",
    "decode_text",
    "dedupe_facts",
    "facts_from_image",
    "facts_from_profile",
    "facts_from_reading",
    "facts_from_text",
    "ground_facts",
    "numbers_in_text",
    "parse_question",
    "profile_csv",
    "read_chart",
    "source_numbers",
    "to_identifier",
    "validate_image",
]
