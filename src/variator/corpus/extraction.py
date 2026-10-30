"""Deterministic extraction of directly supported factor observations."""

from __future__ import annotations

import re
from typing import Any

from variator.corpus.factors import FACTOR_BY_ID
from variator.corpus.models import CorpusDocument, FactorObservation


def make_observation(
    *,
    factor_id: str,
    entity_id: str,
    entity_revision: int,
    role: str,
    source_doc_id: str,
    extraction_rule_id: str,
    confidence: str = "direct",
    value: float | None = None,
    unit: str | None = None,
) -> FactorObservation:
    """Build a validated factor observation."""
    catalog_entry = FACTOR_BY_ID.get(factor_id)
    if catalog_entry is None:
        raise ValueError(f"unknown factor_id: {factor_id}")
    if role not in catalog_entry.permitted_roles:
        raise ValueError(f"role {role!r} is not permitted for {factor_id}")
    return FactorObservation(
        factor_id=factor_id,
        entity_id=entity_id,
        entity_revision=entity_revision,
        role=role,  # type: ignore[arg-type]
        value=value,
        unit=unit,
        confidence=confidence,  # type: ignore[arg-type]
        source_doc_id=source_doc_id,
        extraction_rule_id=extraction_rule_id,
    )


_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("financial.revenue", r"revenue[^0-9$€£]*[$€£]?([0-9][0-9,]*(?:\.[0-9]+)?)", "currency", "outcome"),
    ("financial.growth", r"growth[^0-9-]*(-?[0-9]+(?:\.[0-9]+)?)%", "percent", "outcome"),
    ("commercial.pipeline", r"pipeline[^0-9$€£]*[$€£]?([0-9][0-9,]*(?:\.[0-9]+)?)", "currency", "driver"),
    ("commercial.sla_risk", r"sla risk[^0-9]*([0-9]+(?:\.[0-9]+)?)", "score", "risk"),
    ("product.latency", r"latency[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*ms", "milliseconds", "outcome"),
    ("operations.incident_rate", r"incident rate[^0-9]*([0-9]+(?:\.[0-9]+)?)", "count", "risk"),
    ("people.headcount", r"headcount[^0-9]*([0-9]+)", "count", "outcome"),
    ("market.pricing", r"price(?:d|ing)?[^0-9$€£]*[$€£]?([0-9]+(?:\.[0-9]+)?)", "currency", "driver"),
    ("risk.security", r"security risk", "score", "risk"),
    ("lever.pricing", r"pricing lever", "percent", "lever"),
)


def extract_factor_observations(
    document: CorpusDocument,
    *,
    metadata: dict[str, Any] | None = None,
    entity_id: str | None = None,
) -> list[FactorObservation]:
    """Extract only explicitly structured or pattern-supported factors.

    The extractor deliberately has a small, visible rule table. It never turns
    an arbitrary sentence into a factor and therefore cannot invent causal or
    numeric evidence.
    """
    metadata = metadata or document.provenance.get("metadata", {})
    target = entity_id or (
        document.entities[0] if document.entities else "redwood:organization:company:redwood"
    )
    observations: list[FactorObservation] = []
    structured = metadata.get("factors", []) if isinstance(metadata, dict) else []
    if isinstance(structured, list):
        for item in structured:
            if not isinstance(item, dict):
                continue
            observations.append(
                make_observation(
                    factor_id=str(item["factor_id"]),
                    entity_id=str(item.get("entity_id", target)),
                    entity_revision=int(item.get("entity_revision", 1)),
                    role=str(item.get("role", "outcome")),
                    source_doc_id=document.doc_id,
                    extraction_rule_id=str(item.get("extraction_rule_id", "fixture.structured.v1")),
                    confidence=str(item.get("confidence", "direct")),
                    value=float(item["value"]) if item.get("value") is not None else None,
                    unit=item.get("unit"),
                )
            )
    lowered = document.content.casefold()
    seen = {observation.factor_id for observation in observations}
    for factor_id, pattern, unit, role in _PATTERNS:
        if factor_id in seen:
            continue
        match = re.search(pattern, lowered, re.IGNORECASE)
        if not match:
            continue
        value = None if not match.groups() else float(match.group(1).replace(",", ""))
        observations.append(
            make_observation(
                factor_id=factor_id,
                entity_id=target,
                entity_revision=1,
                role=role,
                source_doc_id=document.doc_id,
                extraction_rule_id=f"regex.{factor_id}.v1",
                value=value,
                unit=unit,
            )
        )
    if any(observation.period_start is None for observation in observations) and document.timestamp:
        period = document.timestamp
        observations = [
            observation.model_copy(update={"period_start": period, "period_end": period})
            if observation.period_start is None
            else observation
            for observation in observations
        ]
    return observations
