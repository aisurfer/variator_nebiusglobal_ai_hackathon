"""Rules of the catalogue: parameters, the as-of rule, unknown is not zero, KB ids, the UI contracts."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from variator.metrics.catalog import ids
from variator.metrics.catalog.artifacts import _phrase, as_of_violations, compute_all
from variator.metrics.catalog.datasets import load_history
from variator.metrics.catalog.engine import definition, evaluate, run
from variator.metrics.catalog.finance import burn_display, runway_display
from variator.metrics.catalog.growth import nps_range_note
from variator.metrics.catalog.params import ParamsError, merge_params
from variator.metrics.catalog.registry import BY_ID, CATALOG, brief_layout, default_params, metrics_for
from variator.metrics.results import KbMetricDef, KbMetricResult, LineageNode, MetricResult, status_of

AS_OF = date(2026, 4, 1)
KB_APP = Path(__file__).resolve().parents[3] / "kb" / "app"


# ---------------------------------------------------------------------------------------------- parameters
def test_params_are_json_and_round_trip():
    p = default_params()
    text = json.dumps(p.model_dump(mode="json"))
    again = type(p).model_validate(json.loads(text))
    assert again == p and again.digest() == p.digest()
    assert set(p.metrics) == {m.id for m in CATALOG}


@pytest.mark.parametrize(
    ("metric_id", "bands", "status"),
    [
        ("fin.cash", {"good": 300, "warn": 100}, "good"),  # 400 >= 300
        ("fin.cash", {"good": 1000, "warn": 300}, "warn"),
        ("fin.cash", {"good": 2000, "warn": 1000}, "bad"),
        ("ops.mttr", {"good": 2, "warn": 4}, "good"),  # 1.5 <= 2 (lower is better)
        ("ops.mttr", {"good": 1, "warn": 2}, "warn"),
        ("ops.mttr", {"good": 0.5, "warn": 1}, "bad"),
    ],
)
def test_bands_change_the_status_not_the_value(history, metric_id, bands, status):
    data = load_history(history())
    base = evaluate(BY_ID[metric_id], data, default_params(), AS_OF, computed_at="x")
    params = merge_params(
        default_params(), {"metrics": {metric_id: {"bands": bands, "target": bands["good"]}}}
    )
    ev = evaluate(BY_ID[metric_id], data, params, AS_OF, computed_at="x")
    assert ev.kb.value == base.kb.value
    assert [p.value for p in ev.kb.series] == [p.value for p in base.kb.series]
    assert ev.kb.status == status == ev.result.status
    assert ev.kb.bands.good == bands["good"] and ev.kb.target == bands["good"]


def test_window_change_changes_the_value_and_the_text(history):
    data = load_history(history())
    params = merge_params(default_params(), {"metrics": {"ops.incidents": {"window_days": 10}}})
    ev = evaluate(BY_ID["ops.incidents"], data, params, AS_OF, computed_at="x")
    assert ev.kb.value == pytest.approx(3.0)  # only INC-2 (2026-03-25) is in 10 days: 1 x 30 / 10
    assert ev.kb.title == "Incidents · 10 days"
    assert any("10 days" in x for x in ev.kb.how_calculated)


def test_status_of_mirrors_plancy():
    from variator.metrics.results import Bands

    assert status_of(None, "higher", Bands(good=1, warn=0)) == "unknown"
    assert status_of(5, "context", Bands(good=1, warn=0)) == "neutral"
    assert status_of(5, "higher", None) == "neutral"


def test_bad_overrides_raise():
    with pytest.raises(ParamsError, match="unknown metric"):
        merge_params(default_params(), {"metrics": {"nope": {}}})
    with pytest.raises(ParamsError, match="unknown option"):
        merge_params(default_params(), {"metrics": {"fin.runway": {"options": {"cap": 1}}}})
    with pytest.raises(ParamsError):
        merge_params(default_params(), {"metrics": {"fin.runway": {"bands": {"good": 1}}}})


# ---------------------------------------------------------------------------------------------- as of
def test_rows_after_t_are_dropped(history):
    data = load_history(history(nps=True, monitoring=True))
    assert data.dropped_after_t == {
        "sim_bank_entry": 1,
        "sim_invoice": 1,
        "sim_incident": 1,
        "monitoring_daily": 1,
        "nps_responses": 1,
    }
    cut = data.t_start
    for table in data.sim.values():
        assert all(v.ts < cut for _, v in table.versions(cut + timedelta(days=999)))


def test_as_of_value_equals_value_on_a_history_cut_at_that_date(history):
    """The Plancy T09 rule: a metric as of D on the full history = the metric on the history known up to D."""
    path = history(nps=True, monitoring=True)
    full = load_history(path)
    params = default_params()
    for d in (date(2026, 2, 15), date(2026, 3, 10), date(2026, 3, 31)):
        cut = load_history(path, as_of_anchor=d + timedelta(days=1))
        for m in CATALOG:
            a, b = run(m, full, params, d), run(m, cut, params, d)
            assert a.value == b.value, (m.id, d)
            assert [i.kb_id for i in a.items] == [i.kb_id for i in b.items], (m.id, d)


def test_no_record_after_the_date(history):
    data = load_history(history(nps=True, monitoring=True))
    for d in (date(2026, 2, 15), AS_OF):
        for ev in compute_all(data, default_params(), d, computed_at="x").values():
            assert as_of_violations(ev) == [], ev.defn.id


def test_dates_after_t_are_unknown(history):
    data = load_history(history())
    c = run(BY_ID["fin.cash"], data, default_params(), date(2026, 4, 10))
    assert c.value is None and "after T" in c.unknown_reason


# ---------------------------------------------------------------------------------------------- unknown
@pytest.mark.parametrize(
    ("metric_id", "drop"),
    [
        ("cust.nps", ()),
        ("ops.sla", ()),
        ("ops.latency", ()),
        ("fin.cash", ("bank_entry",)),
        ("fin.revenue", ("payroll",)),
        ("ops.mttr", ("incident",)),
        ("ops.team_load", ("sprint_load",)),
        ("ops.throughput", ("issues",)),
        ("ppl.headcount", ("people",)),
    ],
)
def test_missing_dataset_gives_unknown_with_a_reason(history, metric_id, drop):
    data = load_history(history(drop=drop))
    ev = evaluate(BY_ID[metric_id], data, default_params(), AS_OF, computed_at="x")
    assert ev.kb.value is None and ev.kb.status == "unknown"
    assert ev.kb.caveats and ev.result.lineage.unknown_reason
    assert all(p.value is None for p in ev.kb.series)  # unknown is not zero


def test_sla_unknown_still_lists_incidents_as_context(history):
    ev = evaluate(BY_ID["ops.sla"], load_history(history()), default_params(), AS_OF, computed_at="x")
    assert ev.kb.value is None and "monitoring_daily" in ev.result.coverage.unknown_components
    assert [i.id for i in ev.kb.items] == ["sim.INC-2", "sim.INC-1"]


# ---------------------------------------------------------------------------------------------- KB ids
def test_id_rules():
    assert ids.sim_id("INV-00082") == "sim.INV-00082"
    assert ids.sim_id("mail.msg-000001") == "mail.msg-000001"  # already a KB id
    assert ids.jira_id("PLAT-67") == "jira.PLAT-67"
    assert ids.mr_id("larchwood/platform", 12) == "gitlab.larchwood-platform.mr-12"
    assert ids.mail_id("msg-000123") == "mail.msg-000123"
    assert ids.person_id("LW-E03") == "person.lw-e03"
    assert ids.org_id("CL-001") == "org.CL-001"
    assert ids.cause_ref("life_event:LE-0019") == ("life_event", "sim.LE-0019")
    assert ids.cause_ref("process:jira.PLAT-4") == ("process", "jira.PLAT-4")
    for good in ("sim.CTR-0001", "jira.IMPL-1", "gitlab.a-b.mr-3", "mail.msg-1", "person.lw-e01", "org.CL-1"):
        assert ids.is_kb_id(good)
    for bad in ("CTR-0001", "jira.plat-1", "person.LW-E01", ""):
        assert not ids.is_kb_id(bad)


def test_id_rules_match_the_kb_adapters():
    pytest.importorskip("yaml")
    sys.path.insert(0, str(KB_APP))
    try:
        sim = pytest.importorskip("kb.connectors.sim")
        directory = pytest.importorskip("kb.connectors.directory")
        email = pytest.importorskip("kb.connectors.email")
        gitlab = pytest.importorskip("kb.connectors.gitlab")
    finally:
        sys.path.remove(str(KB_APP))
    for raw in ("INV-00082", "BNK-00000", "CB-2025-10", "TAX-PAYE-2025-10", "jira.PLAT-4", "a b/c"):
        assert ids.sim_id(raw) == sim.sim_entity_id(raw)
    for pid in ("LW-E03", "CNT-CL-001-1"):
        assert ids.person_id(pid) == directory.person_entity_id(pid)
    assert ids.org_id("CL-001") == directory.organization_entity_id("CL-001")
    assert ids.mail_id("msg-000123") == email.message_entity_id("msg-000123")
    assert ids.mr_id("larchwood/platform", 7) == f"gitlab.{gitlab.slug('larchwood/platform')}.mr-7"


def test_items_and_lineage_carry_kb_ids(history):
    data = load_history(history(nps=True, monitoring=True))
    evals = compute_all(data, default_params(), AS_OF, computed_at="x")
    seen = set()
    for ev in evals.values():
        for item in ev.kb.items:
            assert ids.is_kb_id(item.id), (ev.defn.id, item.id)
            seen.add(item.id.split(".")[0])
        for r in ev.result.highlight:
            assert r.detail is not None and r.detail.id == r.id and r.evidence_ids == [r.id]
            assert r.detail.kind == "kbEntity"  # the workspace opens the KB dossier of the entity
    assert {"sim", "jira", "person"} <= seen
    nps = evals["cust.nps"].result.highlight
    assert nps and {r.dataset for r in nps} == {"nps"}


# ---------------------------------------------------------------------------------------------- contracts
def _walk(node: LineageNode):
    yield node
    for c in node.children:
        yield from _walk(c)


def test_output_validates_against_results_py(history):
    data = load_history(history(nps=True, monitoring=True))
    for ev in compute_all(data, default_params(), AS_OF, computed_at="x").values():
        res = ev.result.to_json_dict()
        assert MetricResult.model_validate(res).to_json_dict() == res
        kb = ev.kb.to_json_dict()
        assert KbMetricResult.model_validate(kb).to_json_dict() == kb
        # the one MVP role covers every metric; the area places it on the brief
        assert kb["roles"] == ["ceo"] and kb["area"] in ("money", "clients", "delivery")
        assert "asOf" in kb and "howCalculated" in kb and "itemsTotal" in kb
        assert ev.result.lineage.kind == "metric" and ev.result.lineage.id == "root"
        ids_ = [n.id for n in _walk(ev.result.lineage)]
        assert len(ids_) == len(set(ids_))


def test_definitions_validate_and_the_ceo_covers_every_metric():
    p = default_params()
    for m in CATALOG:
        d = definition(m, p.of(m.id)).to_json_dict()
        assert KbMetricDef.model_validate(d).to_json_dict() == d
        assert d["roles"] == ["ceo"]
        texts = [d["title"], d["definition"], d.get("itemsLabel") or "", *d["howCalculated"]]
        assert not [t for t in texts if "{" in t], m.id  # every parameter name in the texts is filled
    assert [m.id for m in metrics_for("ceo")] == [m.id for m in CATALOG] and len(CATALOG) == 23
    assert BY_ID["fin.runway"].unit == "months" and BY_ID["ops.latency"].unit == "ms"


def test_the_ceo_brief_layout_places_every_metric_once():
    top, areas = brief_layout()
    placed = top + [i for a in areas for i in a["metrics"]]
    assert sorted(placed) == sorted(m.id for m in CATALOG) and len(set(placed)) == len(placed)
    assert 4 <= len(top) <= 6
    assert [(a["id"], a["title"]) for a in areas] == [
        ("money", "Money"),
        ("clients", "Clients and growth"),
        ("delivery", "Delivery and service"),
    ]
    assert all(BY_ID[i].area == a["id"] for a in areas for i in a["metrics"])
    assert {i for i in placed if BY_ID[i].area == "money"} == {m.id for m in CATALOG if m.family == "finance"}


def test_display_hints_where_the_number_alone_misleads():
    # a runway at its cap is a lower bound: «60+ mo» on the tile, «over 5 years» in the headline
    capped = runway_display(Decimal(60), Decimal(60), growing=True)
    assert capped is not None and capped["value"] == "60+ mo" and capped["phrase"] == "runway over 5 years"
    assert "profitable" in capped["note"] and "capped at 60 months" in capped["note"]
    assert runway_display(Decimal("12.3"), Decimal(60), growing=False) is None
    assert runway_display(Decimal(18), Decimal(18), growing=True)["phrase"] == "runway over 18 months"
    # a negative burn is a net inflow: shown negated, as the net cash flow of a month
    inflow = burn_display(Decimal("-48208.33"))
    assert inflow is not None and inflow["negate"] is True and inflow["label"] == "Net cash flow per month"
    assert inflow["phrase"] == "net cash flow +$48,208 a month" and "-" not in inflow["phrase"]
    assert burn_display(Decimal("1200")) is None and burn_display(Decimal(0)) is None
    # NPS is an index from −100 to 100, not a 0–100 score
    assert BY_ID["cust.nps"].unit == "index" and BY_ID["cust.nps"].gen_unit == "index"
    assert nps_range_note(9, 6).startswith("NPS runs from −100 to 100")
    # the headline reads the phrase of the hint
    ev = SimpleNamespace(
        computation=SimpleNamespace(display=capped),
        kb=SimpleNamespace(title="Runway", value=60.0, unit="months", status="good"),
    )
    assert _phrase(ev) == "runway over 5 years"
    ev.computation.display = None
    assert _phrase(ev) == "runway 60 months"  # the number of the tile («60 mo»)


def _status_agrees(kb) -> str | None:
    """None when the status agrees with the value against the shown target and direction, else why not."""
    if kb.value is None or kb.target is None or kb.direction == "context":
        return None
    meets = kb.value >= kb.target if kb.direction == "higher" else kb.value <= kb.target
    if meets != (kb.status == "good"):
        return f"{kb.id}: value {kb.value}, target {kb.target} ({kb.direction}), status {kb.status}"
    return None


def test_every_status_agrees_with_its_value_target_and_direction(history):
    """A card shows «On track · target X»: on track exactly when the value meets X, for every metric."""
    p = default_params()
    for m in CATALOG:  # the defaults: the good band is the target
        mp = p.of(m.id)
        if mp.target is not None and mp.bands is not None and m.direction != "context":
            assert mp.bands.good == mp.target, m.id
    data = load_history(history(nps=True, monitoring=True))
    for as_of in (AS_OF, AS_OF - timedelta(days=30)):
        evals = compute_all(data, p, as_of, computed_at="x")
        assert [w for w in (_status_agrees(e.kb) for e in evals.values()) if w] == []
        assert all(e.kb.status == e.result.status for e in evals.values())
    # an override whose bands disagree with its target still gives an honest status
    for target, bands, want in (
        (400, (300, 100), "good"),
        (500, (300, 100), "warn"),
        (500, (1000, 450), "bad"),
    ):
        params = merge_params(
            p, {"metrics": {"fin.cash": {"target": target, "bands": {"good": bands[0], "warn": bands[1]}}}}
        )
        kb = evaluate(BY_ID["fin.cash"], data, params, AS_OF, computed_at="x").kb
        assert (kb.value, kb.status) == (400.0, want) and _status_agrees(kb) is None


def test_gross_margin_counts_subcontractors_as_cost_of_revenue(history):
    """Subcontractors deliver client work: their bills are cost of revenue, as in the case model."""
    data = load_history(history())
    ev = evaluate(BY_ID["fin.gross_margin"], data, default_params(), AS_OF, computed_at="x")
    assert ev.kb.value == 72.2  # (500 - 20 - 69 - 50) / 500
    assert ev.kb.target == 30.0 and ev.kb.bands.good == 30.0 and ev.kb.bands.warn == 20.0
    assert "Without subcontractors the margin would be 82.2%." in ev.kb.caveats
    steps = [c.label for c in ev.result.lineage.children[0].children]
    assert steps == ["Revenue", "Cloud (infrastructure)", "Delivery payroll", "Subcontractors"]
    assert {b.key: b.value for b in ev.kb.breakdown}["subcontractors"] == 10.0
    assert "assumption" in " ".join(ev.kb.how_calculated)


def test_display_hints_reach_the_card(history):
    data = load_history(history(nps=True, monitoring=True))
    evals = compute_all(data, default_params(), AS_OF, computed_at="x")
    nps = evals["cust.nps"].kb.to_json_dict()
    assert nps["unit"] == "index" and nps["display"]["note"].startswith("NPS runs from −100 to 100")
    assert KbMetricResult.model_validate(nps).to_json_dict() == nps
    for mid in ("fin.burn", "fin.runway"):
        kb, comp = evals[mid].kb, evals[mid].computation
        # a hint appears exactly when the computation sets one; the phrase stays out of the card
        assert (kb.display is None) == (comp.display is None)
        assert "phrase" not in kb.to_json_dict().get("display", {})


def test_lineage_reaches_events_and_causes(history):
    data = load_history(history())
    ev = evaluate(BY_ID["rev.churn"], data, default_params(), AS_OF, computed_at="x")
    kinds = [n.kind for n in _walk(ev.result.lineage)]
    assert kinds[:3] == ["metric", "operation", "aggregate"] and "event" in kinds
    causes = [n.cause.id for n in _walk(ev.result.lineage) if n.kind == "cause"]
    assert causes == ["sim.LE-1", "sim.RULE-CLIENT-NOTICE"]
    agg = ev.result.lineage.children[0].children[0]
    assert agg.records_total == 1 and agg.records[0].id == "sim.CTR-2"
    # events and causes read as words for people: no record id, no snake_case code, no timestamp
    for n in _walk(ev.result.lineage):
        if n.kind in ("event", "cause"):
            assert "sim." not in n.explanation and "_" not in n.explanation and not re.search(r"\dT\d", n.explanation)
    event = next(n for n in _walk(ev.result.lineage) if n.kind == "event")
    who, what, day = event.explanation.split(" · ")
    assert what == "Contract terminated" and len(day) == 10 and who
    req = evaluate(BY_ID["ops.open_requisitions"], data, default_params(), AS_OF, computed_at="x")
    assert [n.cause.id for n in _walk(req.result.lineage) if n.kind == "cause"] == ["sim.DEC-1"]


def test_changes_compare_with_the_previous_week(history):
    data = load_history(history())
    ev = evaluate(BY_ID["ops.incidents"], data, default_params(), date(2026, 3, 22), computed_at="x")
    assert ev.result.changes.compared_to == "2026-03-15"
    assert [r.id for r in ev.result.changes.added] == ["sim.INC-1"]
