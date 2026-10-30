"""Pure helpers of the causal engine: no database needed."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kb.causal._base import (
    fields_match,
    fmt_dt,
    hub_weight,
    key_fields,
    sensitivity_hit,
    short,
    split_version,
    visible_fields,
)
from kb.causal.explain import origin_phrase, status_phrase
from kb.causal.status import explain_change
from kb.causal.trace import _chains, _Graph

MSK = ZoneInfo("Europe/Moscow")


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_split_version():
    assert split_version("jira.FP-1@3") == ("jira.FP-1", 3)
    assert split_version("person.a@b@12") == ("person.a@b", 12)
    assert split_version("jira.FP-1") is None and split_version("x@") is None and split_version("@3") is None


def test_sensitivity_and_fields():
    assert sensitivity_hit(["status", "summary"], None) == ["status", "summary"]
    assert sensitivity_hit(["status", "summary"], ["summary", "description"]) == ["summary"]
    assert sensitivity_hit(["status"], []) == []
    assert fields_match(None, ["status"]) and fields_match(["rules"], ["*"])
    assert fields_match(["rules"], ["rules", "x"]) and not fields_match(["rules"], ["owner"])
    assert visible_fields(["@relations", "@relations_hash", "title", "@deleted"]) == ["relations", "title", "deleted"]


def test_hub_weight_penalises_hubs():
    assert hub_weight(1) == 1.0
    assert hub_weight(10) < hub_weight(2) < hub_weight(1)
    assert hub_weight(1, 0.5) == 0.5


def test_key_fields_and_short():
    kf = key_fields("Meeting", {"held_at": "2026-03-02T11:00:00+03:00", "attendees": ["A", "B"], "agenda": "x",
                                "@relations_hash": "h"})
    assert kf == {"held_at": "2026-03-02T11:00:00+03:00", "attendees": ["A", "B"]}
    generic = key_fields("Unknown", {"a": 1, "b": "x", "c": {"nested": 1}, "@x": 2})
    assert generic == {"a": 1, "b": "x"}
    assert short("x" * 400).endswith("…") and len(short("x" * 400)) == 280
    assert short(list(range(20))) == list(range(12))


def test_fmt_dt():
    assert fmt_dt(dt("2026-03-02T08:00:00+00:00"), MSK) == "2026-03-02 11:00 MSK"
    assert fmt_dt("2026-03-02T11:00:00+03:00", MSK) == "2026-03-02 11:00 MSK"
    assert fmt_dt(dt("2026-03-01T21:00:00+00:00"), MSK) == "2026-03-02"
    assert fmt_dt("next week", MSK) == "next week" and fmt_dt(None, MSK) == "—"


def _v(seq, op="update", changed=(), rec="2026-03-0{}T10:00:00+03:00"):
    return {"entity_id": "X", "version_id": f"X@{seq}", "seq": seq, "op": op, "changed_fields": list(changed),
            "valid_from": dt(rec.format(seq)), "recorded_at": dt(rec.format(seq))}


def test_explain_change():
    versions = [_v(1, "create"), _v(2, changed=["status"]), _v(3, changed=["statement", "title"]), _v(4, changed=["owner"])]
    link = {"cause_seq": 1, "sensitivity": ["statement"], "status": "STALE"}
    ch = explain_change(link, versions)
    assert ch["changed_fields"] == ["statement"] and ch["changed_by_version"]["version_id"] == "X@3"
    assert ch["cause_current"]["version_id"] == "X@4"
    assert set(ch["other_changed_fields"]) == {"status", "title", "owner"}
    # as known at K (before v3): only the status change is known
    k = dt("2026-03-02T12:00:00+03:00")
    ch = explain_change({**link, "status": "CURRENT_COMPATIBLE"}, versions, k)
    assert ch["changed_fields"] == [] and ch["changed_by_version"] is None and ch["cause_current"]["version_id"] == "X@2"
    broken = explain_change({"cause_seq": 1, "sensitivity": None, "status": "BROKEN"},
                            versions + [_v(5, "delete", ["@deleted"])])
    assert broken["changed_by_version"]["version_id"] == "X@5"


def test_phrases():
    assert "by the editor user:a" in origin_phrase("declared:editor", "user:a")
    assert "Jira" in origin_phrase("declared:jira_import")
    assert "not reviewed" in origin_phrase("inferred:llm:nemotron")
    nodes = {"X@1": {"title": "Policy"}}
    stale = {"status": "STALE", "cause_version": "X@1", "cause_entity_id": "X", "cause_current_version": "X@3",
             "cause_seq": 1, "changed_fields": ["rules"], "sensitivity": ["rules"],
             "changed_by_version": {"seq": 2, "effective_from": dt("2026-05-10T10:00:00+03:00"),
                                    "recorded_at": dt("2026-05-10T10:00:00+03:00")}}
    text = status_phrase(stale, nodes, None, MSK)
    assert text.startswith("now the dependency is stale (STALE)") and "v2" in text and "v3" in text and "rules" in text
    at_k = status_phrase({**stale, "status": "CURRENT", "status_now": "STALE"}, nodes, dt("2026-05-01T00:00:00+03:00"), MSK)
    assert at_k.startswith("as of 2026-05-01") and "the dependency holds" in at_k and "by current knowledge — STALE" in at_k


def _link(lid, eff, effseq, cause, cseq, pred="caused_by", inferred=False, status="CURRENT"):
    return {"link_id": lid, "effect_entity_id": eff, "effect_version": f"{eff}@{effseq}", "effect_seq": effseq,
            "cause_entity_id": cause, "cause_version": f"{cause}@{cseq}", "cause_seq": cseq, "predicate": pred,
            "inferred": inferred, "status": status, "depth": 1}


def _node(eid, seq, kind="Task"):
    return {"entity_id": eid, "seq": seq, "kind": kind, "version_id": f"{eid}@{seq}"}


def test_chains_follow_version_rule_and_guard_cycles():
    # A@2 <- B@1 <- C@1 ; A@1 <- D@1 ; B@1 <- A@1 (entity-level cycle through an inferred candidate)
    links = [_link("l1", "A", 2, "B", 1), _link("l2", "B", 1, "C", 1, "decided_in"), _link("l3", "A", 1, "D", 1),
             _link("l4", "B", 1, "A", 1, inferred=True)]
    nodes = {v["version_id"]: v for v in [_node("A", 2), _node("A", 1), _node("B", 1), _node("C", 1, "Meeting"),
                                          _node("D", 1)]}
    g = _Graph(links, "back")
    assert {x["link_id"] for x in g.children("A", 1)} == {"l3"}  # A@1 does not own links asserted by A@2
    assert {x["link_id"] for x in g.children("A", 2)} == {"l1", "l3"}
    chains, truncated = _chains(g, nodes, "A@2", lambda v: v in ("C@1", "D@1"))
    assert not truncated
    texts = [c["versions"] for c in chains]
    assert ["A@2", "B@1", "C@1"] in texts and ["A@2", "D@1"] in texts
    assert chains[0]["versions"] == ["A@2", "B@1", "C@1"]  # through a meeting first
    assert all("A@1" not in c["versions"][1:] for c in chains)  # the cycle back to A is not followed
