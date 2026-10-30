"""Delivery writer: deterministic bytes, order, version chains, manifest, the sample organisation."""

import gzip
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from variator.orgsim.delivery import (
    MANIFEST,
    TIMELINE,
    check_lines,
    finalize_lines,
    read_timeline,
    verify_delivery,
    write_delivery,
)
from variator.orgsim.events import Event
from variator.orgsim.sample import build_sample, write_sample_delivery

TZ = ZoneInfo("Europe/Berlin")
SOURCES = {
    "tenant": {"id": "acme", "name": "Acme"},
    "sources": [{"id": "sim-acme", "provider": "variator", "name": "Simulation"}],
}
PEOPLE = {"admins": ["user:admin"], "groups": {}, "people": [], "organizations": [], "contacts": []}


def lines(n: int = 6) -> list[dict]:
    out = []
    for i in range(n):
        e = Event(
            run="r",
            option="base",
            scenario=0,
            ts=datetime(2026, 10, 20, 9, 0, tzinfo=TZ) + timedelta(hours=i % 3),
            process="billing",
            kind="invoice",
            entity_id=f"inv-{i % 2}",
            fields={"n": i},
            seq=i,
        )
        out.append(e.to_timeline_line(tenant="acme", source="sim-acme", object_type="invoice"))
    return out


def files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_writing_twice_gives_the_same_bytes(tmp_path):
    for name in ("a", "b"):
        write_delivery(tmp_path / name, tenant="acme", sources=SOURCES, people=PEOPLE, lines=lines(), seed=3)
    assert files(tmp_path / "a") == files(tmp_path / "b")
    raw = (tmp_path / "a" / TIMELINE).read_bytes()
    assert raw[4:8] == b"\x00\x00\x00\x00"  # gzip mtime 0


def test_lines_are_ordered_numbered_and_chained(tmp_path):
    write_delivery(tmp_path, tenant="acme", sources=SOURCES, people=PEOPLE, lines=lines(), seed=3)
    got = read_timeline(tmp_path)
    assert [ln["sequence"] for ln in got] == list(range(1, 7))
    ats = [datetime.fromisoformat(ln["at"]) for ln in got]
    assert ats == sorted(ats)
    # equal times keep the input order: payload n 0 then 3 at 09:00
    assert [ln["payload"]["fields"]["n"] for ln in got] == [0, 3, 1, 4, 2, 5]
    by_obj: dict[str, list[dict]] = {}
    for ln in got:
        by_obj.setdefault(ln["external_id"], []).append(ln)
    for chain in by_obj.values():
        assert [ln["version"] for ln in chain] == list(range(1, len(chain) + 1))
        assert chain[0].get("previous_event_id") is None
        assert all(b["previous_event_id"] == a["event_id"] for a, b in zip(chain, chain[1:], strict=False))
    assert len({ln["event_id"] for ln in got}) == 6
    assert all(v["status"] == "ok" for v in check_lines(got, SOURCES).values())


def test_manifest_counts_and_checksums(tmp_path):
    m = write_delivery(
        tmp_path,
        tenant="acme",
        sources=SOURCES,
        people=PEOPLE,
        lines=lines(),
        seed=3,
        gold_links=[
            {"effect": "a", "cause": "b", "predicate": "caused_by", "expressed_as": "text", "explicit": False}
        ],
    )
    assert (
        m["contract"] == "plancy-kb-corpus/1"
        and m["seed"] == 3
        and m["generator"]["name"] == "variator.orgsim"
    )
    assert (
        m["counts"]["events"] == 6 and m["counts"]["objects"] == 2 and m["counts"]["links"]["implicit"] == 1
    )
    assert json.loads((tmp_path / MANIFEST).read_text()) == m
    assert m["files"][TIMELINE]["records"] == 6
    with gzip.open(tmp_path / TIMELINE, "rb") as fh:
        import hashlib

        assert hashlib.sha256(fh.read()).hexdigest() == m["files"][TIMELINE]["sha256_unpacked"]
    assert verify_delivery(tmp_path) == []
    (tmp_path / "cases.jsonl").write_text('{"id": "x"}\n')
    assert verify_delivery(tmp_path) == ["cases.jsonl: sha256 differs from the manifest"]


def test_writer_refuses_a_foreign_tenant_and_naive_times(tmp_path):
    with pytest.raises(ValueError):
        write_delivery(tmp_path, tenant="other", sources=SOURCES, people=PEOPLE, lines=[], seed=1)
    bad = lines(1)
    bad[0]["at"] = "2026-10-20T09:00:00"
    with pytest.raises(ValueError):
        finalize_lines(bad, "acme")


def test_check_lines_finds_broken_order_and_unknown_sources():
    got = finalize_lines(lines(), "acme")
    got[2]["at"] = "2026-10-19T09:00:00+02:00"
    got[3]["source"] = "nowhere"
    res = check_lines(got, SOURCES)
    assert res["order"]["lines"] == [3] and res["source_declared"]["lines"] == [4]


@pytest.mark.parametrize("anchor", [date(2026, 10, 27), date(2027, 3, 1)])
def test_sample_is_valid_and_relative_to_the_anchor(tmp_path, anchor):
    m = write_sample_delivery(tmp_path, anchor=anchor)
    assert verify_delivery(tmp_path) == []
    assert all(v["status"] == "ok" for v in m["checks"].values())
    got = read_timeline(tmp_path)
    assert len(got) == m["counts"]["events"] == 16
    assert all(datetime.fromisoformat(ln["at"]).date() < anchor for ln in got)
    assert anchor - datetime.fromisoformat(got[0]["at"]).date() < timedelta(days=21)
    issue = [ln for ln in got if ln["external_id"] == "ENG-1"][-1]["payload"]
    assert (
        issue["fields"]["status"]["name"]
        == issue["changelog"]["histories"][-1]["items"][-1]["toString"]
        == "Done"
    )
    assert m["sample_of"] == "wp3sample" and (tmp_path / "dashboard.json").is_file()


def test_sample_events_carry_causes():
    s = build_sample(date(2026, 10, 27))
    assert len(s["events"]) == len(s["lines"]) == 16
    types = {c.type for e in s["events"] for c in e.causes}
    assert types == {"decision", "process", "life_event", "rule", "hypothesis", "habit"}
