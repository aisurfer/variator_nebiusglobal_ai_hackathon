"""Intake of a delivery (kbsim.intake, validate only): a delivery built from the synthetic tenant passes every check
its size allows, and every corruption of a copy is caught by its own check with the event.

(Plancy ran these tests on a sample of its Russian kedr corpus; this copy builds an English delivery from tests/synth.py.)
"""
from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
import yaml

from kbsim import delivery as D
from kbsim import intake as I
from synth import TENANT, write_tenant

ROLES = [{"id": "ceo", "personId": "person.kd-e01"}, {"id": "sales", "personId": "person.kd-e04"},
         {"id": "engineering", "personId": "person.kd-e03"}]
SMALL = {"case_minimum"}  # a 7-case synthetic delivery cannot meet the per-category minimums of §8.2


def _lines(root: Path) -> list[dict]:
    with gzip.open(root / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def _write_lines(root: Path, lines: list[dict]) -> None:
    with gzip.open(root / "fixtures" / "timeline.jsonl.gz", "wt", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False, separators=(",", ":")) + "\n")


def _cases(root: Path) -> list[dict]:
    return [json.loads(x) for x in (root / "cases.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_cases(root: Path, cases: list[dict]) -> None:
    (root / "cases.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")


@pytest.fixture(scope="module")
def delivery(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("intake")
    paths = write_tenant(root)
    tdir = paths.sim_dir / "tenants" / TENANT
    (tdir / "implicit_links.yaml").write_text(yaml.safe_dump({"links": [
        {"effect_beat": "PAY-03", "cause_beat": "PAY-02", "label": "implicit", "quote": "as I wrote on Thursday"}]}),
        encoding="utf-8")
    orgs = root / "organizations.json"
    orgs.write_text(json.dumps([{"id": "simt", "tenant": TENANT, "name": "Test", "language": "en", "roles": ROLES}]),
                    encoding="utf-8")
    D.build(TENANT, root / "d", sim_dir=paths.sim_dir, kb_dir=paths.kb_dir, organizations=orgs)
    return root / "d"


@pytest.fixture(scope="module")
def clean(delivery):
    return I.validate(delivery, adapters=True)


@pytest.fixture
def copy(delivery, tmp_path):
    dst = tmp_path / "copy"
    shutil.copytree(delivery, dst)
    m = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    m["files_sha256"] = {}  # corruptions change the files on purpose
    (dst / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return dst


def _ok(rep: I.Report, cid: str) -> None:
    c = next(c for c in rep.checks if c.id == cid)
    assert c.status == "ok", (cid, c.items[:3])


def _failed(rep: I.Report, cid: str) -> I.Check:
    c = next(c for c in rep.checks if c.id == cid)
    assert c.status == "fail", (cid, [(x.id, x.total) for x in rep.checks if x.total])
    return c


def test_the_synthetic_delivery_fails_only_on_its_size(clean):
    failed = {c.id for c in clean.checks if c.status == "fail"}
    assert failed == SMALL, [(c.id, c.items[:3]) for c in clean.checks if c.status == "fail"]
    ids = {c.id for c in clean.checks}
    assert {"order", "accounts", "dangling", "status_changelog", "mail_acl", "case_visible", "acl_facts", "adapter",
            "gold_expressed", "availability", "language"} <= ids
    for cid in ("order", "accounts", "dangling", "status_changelog", "mail_acl", "case_visible", "acl_facts",
                "adapter", "gold_expressed", "language", "files"):
        _ok(clean, cid)


def test_texts_in_another_script_are_flagged(copy):
    lines = _lines(copy)
    i = next(i for i, ln in enumerate(lines) if ln["source"].startswith("jira"))
    lines[i]["payload"]["fields"]["description"] = "\u03b1\u03b2\u03b3\u03b4 " * 40  # Greek letters: not English
    _write_lines(copy, lines)
    c = next(c for c in I.validate(copy, adapters=False).checks if c.id == "language")
    assert c.status == "warn" and any(it["line"] == i + 1 for it in c.items)


def test_unsorted_timeline(copy):
    lines = _lines(copy)
    lines[10], lines[11] = lines[11], lines[10]
    if lines[10]["at"] == lines[11]["at"]:
        lines[11] = {**lines[11], "at": "2026-06-01T00:00:00+07:00"}
    _write_lines(copy, lines)
    c = _failed(I.validate(copy, adapters=False), "order")
    assert c.items[0]["line"] in (11, 12) and c.items[0]["object"]


def test_unknown_account(copy):
    lines = _lines(copy)
    i = next(i for i, ln in enumerate(lines) if ln["source"].startswith("jira") and ln["payload"]["fields"].get("assignee"))
    lines[i]["payload"]["fields"]["assignee"] = {"accountId": "712020:ghost", "displayName": "Ghost"}
    _write_lines(copy, lines)
    c = _failed(I.validate(copy, adapters=False), "accounts")
    assert c.items[0]["account"] == "712020:ghost" and c.items[0]["line"] == i + 1


def test_dangling_link(copy):
    lines = _lines(copy)
    i = next(i for i, ln in enumerate(lines) if ln["source"].startswith("jira") and ln["payload"].get("remotelinks"))
    lines[i]["payload"]["remotelinks"][0]["object"]["url"] = "https://simt.atlassian.net/wiki/spaces/GEN/pages/699999"
    _write_lines(copy, lines)
    c = _failed(I.validate(copy, adapters=False), "dangling")
    assert any(it["target"].endswith("/699999") and it["line"] == i + 1 for it in c.items)


def test_status_differs_from_the_changelog(copy):
    lines = _lines(copy)
    i = next(i for i, ln in enumerate(lines) if ln["source"].startswith("jira") and any(
        it.get("field") == "status" for h in ln["payload"]["changelog"]["histories"] for it in h["items"]))
    lines[i]["payload"]["fields"]["status"]["name"] = "Nonsense"
    _write_lines(copy, lines)
    c = _failed(I.validate(copy, adapters=False), "status_changelog")
    assert c.items[0]["status"] == "Nonsense" and c.items[0]["line"] == i + 1


def test_wrong_mail_acl(copy):
    lines = _lines(copy)
    i = next(i for i, ln in enumerate(lines) if ln["source"].startswith("mail") and len(ln["acl"]) >= 2)
    dropped = lines[i]["acl"].pop()
    lines[i]["acl"].append("user:intruder")
    _write_lines(copy, lines)
    c = _failed(I.validate(copy, adapters=False), "mail_acl")
    item = next(it for it in c.items if it["line"] == i + 1)
    assert item["missing"] == [dropped] and item["extra"] == ["user:intruder"]


def test_invisible_evidence(copy):
    cases = _cases(copy)
    k = next(k for k, c in enumerate(cases) if c["category"] not in ("acl", "negative") and c["evidence"]
             and c["evidence"][0]["entity_id"].startswith("mail."))
    cases[k]["principal"] = "user:nobody"  # a principal of no group sees no mail and no container
    _write_cases(copy, cases)
    c = _failed(I.validate(copy, adapters=False), "case_visible")
    assert c.items[0]["case"] == cases[k]["id"]


def test_closed_fact_in_an_open_text(copy):
    cases = _cases(copy)
    acl = next(c for c in cases if c["category"] == "acl" and any(f.get("facts") for f in c["forbidden"]))
    fact = next(f for fb in acl["forbidden"] for f in fb.get("facts") or [])
    form = max(fact["any"], key=len)
    lines = _lines(copy)
    # an open ticket the principal reads (all-staff project): the closed fact appears in its description
    src = yaml.safe_load((copy / "config" / "sources.yaml").read_text(encoding="utf-8"))
    jira = next(s for s in src["sources"] if s["provider"] == "jira")
    open_projects = {k for k, v in jira["containers"].items() if "grp:all-staff" in (v.get("acl") or [])}
    i = next(i for i, ln in enumerate(lines) if ln["source"] == jira["id"] and ln["container"] in open_projects)
    lines[i]["payload"]["fields"]["description"] = f"By the way: {form}."
    _write_lines(copy, lines)
    m = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    m["legitimate_propagation"] = [p for p in m.get("legitimate_propagation") or []
                                   if not (p["case"] == acl["id"] and p["fact"] == fact["key"])]
    (copy / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    c = _failed(I.validate(copy, adapters=False), "acl_facts")
    assert any(it["case"] == acl["id"] and it["fact"] == fact["key"] and it["line"] == i + 1 for it in c.items)


def test_more_corruptions_are_caught(copy):
    lines = _lines(copy)
    lines[5] = {**lines[5], "modified_at": "2026-12-31T00:00:00+07:00"}  # at < modified_at
    gold = (copy / "gold_links.jsonl").read_text(encoding="utf-8").splitlines()
    g = json.loads(gold[0])
    g["expressed_as"] = "reply" if g["expressed_as"] != "reply" else "parent"
    gold[0] = json.dumps(g, ensure_ascii=False)
    (copy / "gold_links.jsonl").write_text("\n".join(gold) + "\n", encoding="utf-8")
    _write_lines(copy, lines)
    (copy / "dashboard.json").unlink()
    rep = I.validate(copy, adapters=False)
    for cid in ("order", "gold_expressed", "files"):
        _failed(rep, cid)
