"""Grounding checks: fact citations in the model and numbers in LLM narrative.

The LLM never computes statistics. These checks make that verifiable: every
variable must cite evidence or admit it is an assumption, and any number the
narrative mentions is matched against the numbers code actually produced.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from itertools import combinations

from pydantic import BaseModel, create_model, model_validator

from variator.domain import DecisionModel, Fact, OptimizationResult, SimulationResult
from variator.reasoning.context import round_sig

FACT_ID_RE = re.compile(r"\bF(\d+)\b")
ASSUMPTION_WORD = "assumption"


def cited_fact_ids(text: str | None) -> list[int]:
    """1-based fact numbers cited in ``text`` (``"[F2][F5]"`` -> ``[2, 5]``)."""
    return [int(m) for m in FACT_ID_RE.findall(text or "")]


def citation_errors(model: DecisionModel, n_facts: int) -> list[str]:
    """Problems with ``Variable.source`` citations, phrased as repair instructions."""
    valid = f"[F1]..[F{n_facts}]" if n_facts else "none (no evidence was provided)"
    errors = []
    for var in model.variables:
        source = (var.source or "").strip()
        ids = cited_fact_ids(source)
        unknown = sorted({i for i in ids if not 1 <= i <= n_facts})
        if unknown:
            errors.append(
                f"variable {var.name!r} cites non-existent facts {[f'F{i}' for i in unknown]}; "
                f"valid fact ids: {valid}"
            )
        elif not ids and ASSUMPTION_WORD not in source.lower():
            errors.append(
                f"variable {var.name!r} source {source!r} must cite fact ids like '[F1]' "
                "or start with 'assumption:'"
            )
    return errors


def grounded_decision_schema(n_facts: int) -> type[DecisionModel]:
    """``DecisionModel`` subclass that also validates citations against ``n_facts``.

    It keeps the name and JSON schema of ``DecisionModel``, so the request is
    identical, but citation errors flow into ``structured()``'s repair loop.
    """

    def _check_citations(self: DecisionModel) -> DecisionModel:
        errors = citation_errors(self, n_facts)
        if errors:
            raise ValueError("; ".join(errors))
        return self

    return create_model(
        "DecisionModel",
        __base__=DecisionModel,
        __doc__=DecisionModel.__doc__,
        __validators__={"_check_citations": model_validator(mode="after")(_check_citations)},
    )


def as_plain_model(model: DecisionModel) -> DecisionModel:
    """Drop a dynamic subclass so callers get an exact ``DecisionModel``."""
    if type(model) is DecisionModel:
        return model
    return DecisionModel.model_validate(model.model_dump())


def evidence_coverage(model: DecisionModel, n_facts: int) -> float | None:
    """Share of variables whose source cites at least one existing fact."""
    if n_facts == 0 or not model.variables:
        return None
    grounded = sum(
        1 for v in model.variables if any(1 <= i <= n_facts for i in cited_fact_ids(v.source))
    )
    return grounded / len(model.variables)


# --- numbers in narrative ------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?:\s?(?P<suffix>%|percent\b|pp\b|k\b|K\b|m\b|M\b|mn\b|bn\b|B\b|million\b|billion\b|thousand\b))?"
)
_SCALE = {
    "k": 1e3, "K": 1e3, "thousand": 1e3,
    "m": 1e6, "M": 1e6, "mn": 1e6, "million": 1e6,
    "bn": 1e9, "B": 1e9, "billion": 1e9,
}
SMALL_INT_LIMIT = 12  # bare integers up to this are counts ("3 years", "top 5"), not statistics
# Method constants the narrative may mention ("worst 5%", "p90", "95% CVaR").
METHOD_CONSTANTS = (5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0, 100.0)


def _sig_digits(int_part: str, frac: str) -> int:
    digits = int_part.replace(",", "")
    if frac:
        return max(len((digits + frac[1:]).lstrip("0")), 1)
    return max(len(digits.lstrip("0").rstrip("0")), 1)


def numbers_in_text(text: str) -> list[tuple[str, float, int]]:
    """Numbers mentioned in prose as ``(raw, value, significant_digits)``.

    Percentages keep their face value (``12%`` -> 12); ``allowed_numbers`` adds
    x100 versions of fractions so they match. Years and small counts are skipped.
    """
    found = []
    for match in _NUMBER_RE.finditer(text):
        int_part, frac, suffix = match.group("int"), match.group("frac") or "", match.group("suffix")
        value = float(int_part.replace(",", "") + frac)
        if suffix is None and not frac:
            if value <= SMALL_INT_LIMIT or (1900 <= value <= 2100 and "," not in int_part):
                continue
        if suffix in _SCALE:
            value *= _SCALE[suffix]
        found.append((match.group(0).strip(), value, _sig_digits(int_part, frac)))
    return found


def allowed_numbers(
    model: DecisionModel | None,
    simulation: SimulationResult | None,
    optimization: OptimizationResult | None = None,
    facts: Sequence[Fact] = (),
) -> list[float]:
    """Every number the narrative may legitimately quote.

    That is: simulation statistics and their pairwise differences across options,
    tornado values, optimizer outputs, model parameters, fact values and numbers in
    fact statements, plus x100 versions of fractions (to match percentages).
    """
    base: list[float] = list(METHOD_CONSTANTS)
    if simulation is not None and simulation.options:
        base.append(float(simulation.n_scenarios))
        stat_fields = [k for k in simulation.options[0].model_dump() if k not in ("option", "histogram")]
        for field in stat_fields:
            column = [float(getattr(o, field)) for o in simulation.options]
            base.extend(column)
            base.extend(a - b for a, b in combinations(column, 2))
        for s in simulation.sensitivities:
            base.extend([s.metric_at_low, s.metric_at_high, s.swing])
        base.extend(simulation.risk_hit_rates.values())
    if optimization is not None:
        base.extend(x for x in (optimization.expected_value, optimization.cvar, optimization.budget_used)
                    if x is not None)
    if model is not None:
        base.extend(_model_numbers(model))
    for fact in facts:
        if fact.value is not None:
            base.append(fact.value)
        base.extend(value for _, value, _ in numbers_in_text(fact.statement))
    out = [abs(x) for x in base if math.isfinite(x)]
    out.extend(x * 100 for x in list(out) if x <= 1)
    return out


def _model_numbers(model: DecisionModel) -> Iterable[float]:
    def dist_numbers(dist: BaseModel) -> Iterable[float]:
        return (v for k, v in dist.model_dump().items() if k != "kind" and isinstance(v, int | float))

    for var in model.variables:
        yield from dist_numbers(var.distribution)
    for option in model.options:
        if option.cost is not None:
            yield option.cost
        for dist in option.overrides.values():
            yield from dist_numbers(dist)
    for risk in model.risks:
        yield risk.probability
        yield from dist_numbers(risk.magnitude)
    for corr in model.correlations:
        yield corr.rho


def ungrounded_numbers(texts: Iterable[str], allowed: Sequence[float]) -> list[str]:
    """Numbers in ``texts`` that match no allowed value at their displayed precision.

    ``$3.2M`` matches 3,214,567 because both round to 3.2e6 at 2 significant digits.
    """
    flagged: list[str] = []
    for text in texts:
        for raw, value, sig in numbers_in_text(text):
            target = round_sig(value, sig)
            if not any(math.isclose(round_sig(a, sig), target, rel_tol=1e-9) for a in allowed):
                if raw not in flagged:
                    flagged.append(raw)
    return flagged
