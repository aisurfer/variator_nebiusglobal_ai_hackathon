"""Delivery of the corpus contract (kbsim.delivery, T07 A3): split, files, bytes, gold links, the closed sample."""
from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from kbsim import contract as K
from kbsim import delivery as D
from kbsim.export import export, read_timeline
from synth import MSG1, MSG2, TENANT, write_tenant

def test_split_is_stratified_and_deterministic():
    cases = [(f"C{i:03d}", cat) for i, cat in enumerate(["why"] * 11 + ["acl"] * 8 + ["impact"] * 3 + ["solo"])]
    a = D.assign_splits(cases)
    assert a == D.assign_splits(list(reversed(cases)))  # order of the input does not matter
    by = Counter((cat, a[cid]) for cid, cat in cases)
    assert (by[("why", "holdout")], by[("acl", "holdout")], by[("impact", "holdout")]) == (3, 2, 1)
    assert by[("solo", "dev")] == 1  # a single case stays in dev


def test_write_splits_keeps_comments_and_layout(tmp_path):
    p = tmp_path / "cases.yaml"
    p.write_text("# header\ncases:\n  # group\n  - id: C1\n    difficulty: trivial\n    category: lookup\n"
                 "    question: q?\n    expect: {}\n    rationale: r\n", encoding="utf-8")
    assert D.write_splits(p, {"C1": "holdout"}) == 1
    assert D.write_splits(p, {"C1": "dev"}) == 1  # rewriting replaces, never duplicates
    text = p.read_text(encoding="utf-8")
    assert text.count("split:") == 1 and "    category: lookup\n    split: dev\n" in text and "# group" in text


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("deliv")
    paths = write_tenant(root)
    tdir = paths.sim_dir / "tenants" / TENANT
    (tdir / "implicit_links.yaml").write_text(yaml.safe_dump({"links": [
        {"effect_beat": "PAY-03", "cause_beat": "PAY-02", "label": "implicit", "quote": "as I wrote on Thursday"},
        {"effect_beat": "PAY-03", "cause_beat": "PAY-01", "label": "implicit", "quote": "this text is not in the e-mail"},
        {"effect_beat": "TL-02", "cause_beat": "TL-01", "label": "none", "quote": None},
    ]}, allow_unicode=True), encoding="utf-8")
    orgs = root / "organizations.json"
    orgs.write_text(json.dumps([{"id": "simt", "tenant": TENANT, "name": "Test", "language": "en", "roles": []}],
                               ensure_ascii=False), encoding="utf-8")
    a = D.build(TENANT, root / "a", sim_dir=paths.sim_dir, kb_dir=paths.kb_dir, organizations=orgs)
    b = D.build(TENANT, root / "b", sim_dir=paths.sim_dir, kb_dir=paths.kb_dir, organizations=orgs)
    export(paths)
    return paths, root / "a", root / "b", a, b


def _files(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_delivery_is_complete_and_byte_identical(built):
    paths, a, b, rep, _ = built
    fa = _files(a)
    assert fa == _files(b)
    for name in ("config/sources.yaml", "config/people.yaml", "fixtures/timeline.jsonl.gz", "cases.jsonl",
                 "gold_links.jsonl", "dashboard.json", "manifest.json", "sample/fixtures/timeline.jsonl.gz",
                 "sample/cases.jsonl", "sample/gold_links.jsonl", "sample/manifest.json", "sample/config/people.yaml"):
        assert name in fa, name
    # the timeline is the one kbsim.export writes into kb/fixtures/<t>/ (bytes, not only content)
    assert fa["fixtures/timeline.jsonl.gz"] == paths.timeline.read_bytes()
    assert fa["config/sources.yaml"] == (paths.config_dir / "sources.yaml").read_bytes()
    man = json.loads(fa["manifest.json"])
    assert man["contract"] == "plancy-kb-corpus/1" and man["language"] == "en" and man["commercial_use_allowed"]
    assert man["files_sha256"]["fixtures/timeline.jsonl.gz"] == D.sha256(fa["fixtures/timeline.jsonl.gz"])
    assert man["inputs_sha256"]["scenario.yaml"] and man["generator"]["source_sha256"]
    assert man["counts"]["events"] == len(read_timeline(paths.timeline))
    assert man["checks"]["implicit_links"] == {"judged_implicit": 2, "judged_none": 1, "kept": 1,
                                               "quote_not_in_payload": 1}
    assert "generated_at" not in fa["cases.jsonl"].decode() and rep["implicit"] == 1
    assert man["checks"]["cases_skipped"] == ["C005"]


def test_cases_follow_the_contract(built):
    paths, a, *_ = built
    resolved = json.loads(paths.resolved.read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (a / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == [c["id"] for c in resolved["cases"] if not c["skip"]]
    assert "C005" not in {r["id"] for r in rows}  # unrealised beats: no artefact to point at
    by = {r["id"]: r for r in rows}
    c1 = by["C001"]
    assert c1["evidence"][0]["entity_id"].startswith("mail.") and c1["evidence"][0]["version"] == 1
    assert c1["evidence"][0]["also"] and c1["language"] == "en" and "split" in c1
    c3 = by["C003"]  # acl: the forbidden letter with its facts
    assert c3["forbidden"][0]["facts"] == [{"key": "salary", "any": ["250,000"]}] and c3["principal"] == "user:i.petrov"
    assert by["C002"]["roots"][0]["entity_id"].startswith("confluence.page-")


def test_gold_links_declared_and_implicit(built):
    _, a, *_ = built
    gold = [json.loads(x) for x in (a / "gold_links.jsonl").read_text(encoding="utf-8").splitlines()]
    got = {(g["effect"], g["predicate"], g["cause"], g["expressed_as"], g["explicit"]) for g in gold}
    page = next(g["cause"] for g in gold if g["predicate"] == "decided_in" and g["effect"].endswith(".dec-1"))
    assert ("jira.MENU-1", "implements", f"{page}.dec-1", "remote_link", True) in got
    assert (f"{page}.dec-1", "decided_in", page, "decision_item", True) in got
    assert (page, "informed_by", "jira.MENU-1", "page_property", True) in got
    assert ("mail." + K.mail_slug("msg-000002"), "replies_to", "mail.msg-000001", "reply", True) in got
    assert any(g["predicate"] == "closes" and g["cause"] == "jira.MENU-1" and g["expressed_as"] == "mr_text"
               for g in gold)
    imp = [g for g in gold if not g["explicit"]]
    assert len(imp) == 1 and imp[0]["effect"] == "jira.MENU-1" and imp[0]["cause"].startswith("confluence.page-")
    assert imp[0]["quote"] == "as I wrote on Thursday" and imp[0]["expressed_as"] == "text"
    assert all(g["expressed_as"] in K.EXPRESSED_AS and g["cause"] for g in gold)
    assert {MSG1, MSG2}  # message ids of the synthetic thread


def test_sample_is_a_closed_slice(built):
    _, a, *_ = built
    s = K.Delivery.load(a / "sample")
    assert s.lines and all(lk.cause for ln in s.lines for lk in s.links(ln))  # nothing points outside the slice
    cases = [json.loads(x) for x in (a / "sample" / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert 1 <= len(cases) <= 20
    for c in cases:
        for e in c["evidence"] + c["roots"] + c["forbidden"]:
            assert e["entity_id"] in s.entities, (c["id"], e["entity_id"])
    with gzip.open(a / "sample" / "fixtures" / "timeline.jsonl.gz", "rt", encoding="utf-8") as fh:
        ats = [json.loads(x)["at"] for x in fh]
    assert ats == sorted(ats)
