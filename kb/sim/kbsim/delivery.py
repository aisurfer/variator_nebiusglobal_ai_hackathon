"""A simulated tenant as a delivery of the corpus contract (Docs/KB/05-CORPUS-CONTRACT.md §1; T07 A3).

    cd kb && PYTHONPATH=sim:app python -m kbsim.export company --delivery <dir>
    cd kb && PYTHONPATH=sim python -m kbsim.delivery split company      # (re)write split: into cases.yaml

<dir>/
  config/{sources,people}.yaml   the tenant config, as kbsim.export writes it into config/tenants/<t>/
  fixtures/timeline.jsonl.gz     the timeline, byte for byte the one of kb/fixtures/<t>/
  cases.jsonl                    cases.resolved.json in the contract format (§8.1) with `split`
  gold_links.jsonl               declared links of the payloads (kbsim.contract) + implicit ones (explicit: false)
  dashboard.json                 the tenant's entry of data/kb/organizations.json
  manifest.json                  contract, generator, seed, input and file hashes, counters, audit, checks
  sample/                        a closed slice of the same world with 10–20 cases (§10)

Everything is a pure function of the inputs: no clock, no randomness, gzip without mtime. Building twice gives the
same bytes. Implicit gold links come from the scenario beats written with `reference_style: implicit`, confirmed
by an independent judge (sim/tenants/<t>/implicit_links.yaml: label and the exact quote); a link is kept only
when its quote is found in the delivered payload of the effect.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

import kbsim
from kbsim import contract as K
from kbsim import resolve as R

SIM_DIR = Path(__file__).resolve().parents[1]
KB_DIR = SIM_DIR.parent
ORGANIZATIONS = KB_DIR.parent / "data" / "kb" / "organizations.json"
HOLDOUT = 0.3
SAMPLE_CASES = (10, 20)
SAMPLE_EVENTS = 900  # upper bound of the sample timeline


# ================================================================================================ helpers
def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def file_sha(path: Path) -> str | None:
    return sha256(path.read_bytes()) if path.is_file() else None


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def dump_jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_gz_lines(path: Path, lines: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        for line in lines:
            gz.write((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            n += 1
    return n


CONSUMERS = {"intake.py"}  # reads a delivery, never writes one


def generator_sha() -> str:
    """Hash of the generator's own source (kbsim/*.py without the tools that only read deliveries)."""
    h = hashlib.sha256()
    for p in sorted(x for x in Path(kbsim.__file__).parent.glob("*.py") if x.name not in CONSUMERS):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


# ================================================================================================ split (§8.2)
def assign_splits(cases: Iterable[tuple[str, str]], holdout: float = HOLDOUT) -> dict[str, str]:
    """dev / holdout by a hash of the case id, stratified by category: round(holdout * n) of each category."""
    by: dict[str, list[str]] = defaultdict(list)
    for cid, cat in cases:
        by[cat].append(cid)
    out: dict[str, str] = {}
    for cat, ids in by.items():
        ranked = sorted(ids, key=lambda c: (sha256(f"split:{c}"), c))
        k = int(len(ranked) * holdout + 0.5)
        for i, c in enumerate(ranked):
            out[c] = "holdout" if i < k else "dev"
    return out


_CASE_START = re.compile(r"^  - id: (?P<id>\S+)\s*$")


def write_splits(path: Path, splits: dict[str, str]) -> int:
    """Write `split:` into cases.yaml after each case's `category:` line, keeping the file's comments and layout."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    cur: str | None = None
    n = 0
    for ln in lines:
        m = _CASE_START.match(ln)
        if m:
            cur = m.group("id")
        if cur and re.match(r"^    split: ", ln):
            continue  # replaced below
        out.append(ln)
        if cur and re.match(r"^    category: ", ln) and cur in splits:
            out.append(f"    split: {splits[cur]}\n")
            n += 1
    path.write_text("".join(out), encoding="utf-8")
    return n


# ================================================================================================ cases (§8.1)
def _version_of(beat: dict[str, Any]) -> int | None:
    prim = beat.get("primary")
    for v in beat.get("versions") or []:
        if R.entity_of(v) == prim:
            return int(v.rpartition("@")[2])
    return None


def _evidence(beat: dict[str, Any], *, facts: bool = False) -> dict[str, Any]:
    e: dict[str, Any] = {"entity_id": beat.get("primary")}
    v = _version_of(beat)
    if v is not None:
        e["version"] = v
    e["why"] = f"{beat['beat']} · {beat.get('channel') or ''}".strip(" ·")
    also = [x for x in beat.get("entities") or [] if x != beat.get("primary")]
    if also:
        e["also"] = also  # the same artefact in another form (the letter and its attachment): any of them counts
    if facts:
        e["facts"] = beat.get("facts") or []
        extra = [x for x in beat.get("all") or [] if x != beat.get("primary") and x not in also]
        if extra:
            e["also_forbidden"] = extra
    return e


def contract_cases(resolved: dict[str, Any], *, language: str) -> list[dict[str, Any]]:
    out = []
    for c in resolved["cases"]:
        if c.get("skip"):
            continue  # its beats were never realised: no artefact to point at (listed in manifest checks)
        out.append({
            "id": c["id"], "category": c["category"], "difficulty": c["difficulty"], "language": language,
            "split": c.get("split"), "question": c["question"], "principal": c["principal"],
            "known_at": c["known_at"],
            "evidence": [_evidence(b) for b in c["evidence"]],
            "roots": [_evidence(b) for b in c["roots"]],
            "forbidden": [_evidence(b, facts=True) for b in c["forbidden"]],
            "answer_facts": c["answer_facts"], "expect_not_found": c["expect_not_found"],
            "rationale": c["rationale"],
        })
    return out


# ================================================================================================ gold links (§6)
def _payload_text(payload: Any) -> str:
    """Every string of a payload (ADF bodies decoded), whitespace-normalised: where a quote must be found."""
    parts: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, str):
            if x.startswith("{") and '"type"' in x:
                try:
                    walk(json.loads(x))
                    return
                except ValueError:
                    pass
            parts.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(payload)
    return _ws(" ".join(parts))


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def implicit_links(resolved: dict[str, Any], judged: list[dict[str, Any]], delivery: K.Delivery,
                   ) -> tuple[list[dict[str, Any]], Counter]:
    """Implicit gold links: judged `implicit` pairs of beats whose quote is in the effect's delivered payload."""
    beats = resolved["beats"]
    by_seq: dict[int, list[int]] = defaultdict(list)
    for i, ln in enumerate(delivery.lines):
        m = re.match(r"seq (\d+) ", str(ln.get("note") or ""))
        if m:
            by_seq[int(m.group(1))].append(i)
    stats: Counter = Counter()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for j in judged:
        stats[f"judged_{j['label']}"] += 1
        if j["label"] != "implicit" or not j.get("quote"):
            continue
        eff, cau = beats.get(j["effect_beat"]), beats.get(j["cause_beat"])
        if not eff or not cau or not eff.get("primary") or not cau.get("primary"):
            stats["unresolved"] += 1
            continue
        idx = [i for s in eff.get("events") or [] for i in by_seq.get(s, [])]
        quote = _ws(j["quote"])
        hit = next((i for i in idx if quote and quote in _payload_text(delivery.lines[i].get("payload"))), None)
        if hit is None:
            stats["quote_not_in_payload"] += 1
            continue
        effect = delivery.entity_of(delivery.lines[hit]) or eff["primary"]  # the text's owner: the page, not its decision
        key = (effect, cau["primary"])
        if key in out or effect == cau["primary"]:
            continue
        out[key] = {"effect": effect, "cause": cau["primary"], "predicate": "refers_to",
                    "expressed_as": "text", "since": delivery.lines[hit]["at"], "explicit": False,
                    "quote": j["quote"], "beats": [j["effect_beat"], j["cause_beat"]]}
        stats["kept"] += 1
    return list(out.values()), stats


def load_judged(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("links") or [])


# ================================================================================================ sample (§10)
def _case_entities(case: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for part in ("evidence", "roots", "forbidden"):
        for e in case.get(part) or []:
            out.add(e["entity_id"])
            out.update(e.get("also") or [])
            out.update(e.get("also_forbidden") or [])
    return out


def _closure(seed: set[str], delivery: K.Delivery, links_by_effect: dict[str, list[str]], owner: dict[str, str],
             lines_by_entity: dict[str, list[int]]) -> tuple[set[str], set[int]]:
    """Entities and timeline lines needed so that every link of an included payload resolves inside the slice."""
    todo = [owner.get(e, e) for e in seed]
    ents: set[str] = set()
    lines: set[int] = set()
    while todo:
        e = todo.pop()
        e = owner.get(e, e)
        if e in ents:
            continue
        ents.add(e)
        for i in lines_by_entity.get(e, []):
            lines.add(i)
        for c in links_by_effect.get(e, []):
            todo.append(c)
    return ents, lines


def sample_slice(cases: list[dict[str, Any]], delivery: K.Delivery, gold: list[dict[str, Any]], *,
                 bounds: tuple[int, int] = SAMPLE_CASES, max_events: int = SAMPLE_EVENTS,
                 always: set[str] | None = None) -> tuple[list[dict[str, Any]], list[int]]:
    """10–20 cases, every category when possible, whose evidence and links stay inside a few hundred events."""
    owner: dict[str, str] = {}  # derived entity (decision, task, attachment) -> its line's primary entity
    lines_by_entity: dict[str, list[int]] = defaultdict(list)
    for i, ln in enumerate(delivery.lines):
        eid = delivery.entity_of(ln)
        if not eid:
            continue
        lines_by_entity[eid].append(i)
        for d in delivery.derived(ln):
            owner[d] = eid
    links_by_effect: dict[str, list[str]] = defaultdict(list)
    for g in gold:
        if g.get("cause"):
            links_by_effect[owner.get(g["effect"], g["effect"])].append(g["cause"])
    for i, ln in enumerate(delivery.lines):  # every other link into the delivery must resolve inside the slice too
        eid = delivery.entity_of(ln)
        if eid:
            links_by_effect[eid].extend(sorted(delivery.references(ln, i)))
    chosen: list[dict[str, Any]] = []
    ents, lines = _closure(set(always or ()), delivery, links_by_effect, owner, lines_by_entity)
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cases:
        by_cat[c["category"]].append(c)
    order = sorted(by_cat)
    rounds = 0
    while len(chosen) < bounds[1] and rounds < 3:
        rounds += 1
        for cat in order:
            if len(chosen) >= bounds[1]:
                break
            for c in sorted(by_cat[cat], key=lambda c: sha256(f"sample:{c['id']}")):
                if c in chosen:
                    continue
                e2, l2 = _closure(_case_entities(c), delivery, links_by_effect, owner, lines_by_entity)
                if len(lines | l2) <= max_events:
                    chosen.append(c)
                    ents |= e2
                    lines |= l2
                    break
        if len(chosen) >= bounds[0] and rounds >= 1 and len(lines) > max_events * 0.6:
            break
    return sorted(chosen, key=lambda c: c["id"]), sorted(lines)


# ================================================================================================ manifest (§11)
def counters(delivery: K.Delivery, gold: list[dict[str, Any]], cases: list[dict[str, Any]],
             people: dict[str, Any]) -> dict[str, Any]:
    by_source = Counter(ln["source"] for ln in delivery.lines)
    by_type = Counter(f"{ln['source']}:{ln['object_type']}" for ln in delivery.lines)
    ops = Counter(ln.get("op", "upsert") for ln in delivery.lines)
    declared = Counter(g["predicate"] for g in gold if g["explicit"])
    expressed = Counter(g["expressed_as"] for g in gold)
    ats = [ln["at"] for ln in delivery.lines]
    return {
        "events": len(delivery.lines), "by_source": dict(sorted(by_source.items())),
        "by_object_type": dict(sorted(by_type.items())), "ops": dict(sorted(ops.items())),
        "entities": len(delivery.entities),
        "period": {"first_at": min(ats) if ats else None, "last_at": max(ats) if ats else None},
        "links": {"declared": sum(declared.values()), "declared_by_predicate": dict(sorted(declared.items())),
                  "implicit": sum(1 for g in gold if not g["explicit"]),
                  "by_expressed_as": dict(sorted(expressed.items())),
                  "unresolved": sum(1 for g in gold if not g.get("cause"))},
        "cases": {"total": len(cases), "by_category": dict(sorted(Counter(c["category"] for c in cases).items())),
                  "by_split": dict(sorted(Counter(str(c.get("split")) for c in cases).items())),
                  "by_difficulty": dict(sorted(Counter(c["difficulty"] for c in cases).items()))},
        "directory": {"people": len(people.get("people") or []), "organizations": len(people.get("organizations") or []),
                      "contacts": len(people.get("contacts") or []), "groups": len(people.get("groups") or {})},
    }


def manifest(*, tenant: str, name: str, language: str, inputs: dict[str, str | None], files: dict[str, str],
             counts: dict[str, Any], checks: dict[str, Any], seed: Any, propagation: list[dict[str, Any]],
             settings: dict[str, Any] | None = None, part_of: str | None = None) -> dict[str, Any]:
    settings = settings or {}
    return {
        "contract": K.CONTRACT,
        "tenant": tenant, "name": name, "language": language,
        **({"reference": True} if settings.get("reference") else {}),
        "generator": {"name": "kbsim", "version": kbsim.__version__, "source_sha256": generator_sha()},
        "seed": seed,
        "datasets": [],  # no external datasets: every text was written by the simulation (Nemotron Lightning)
        "mappings": {}, "time_shifts": {},
        "legitimate_propagation": propagation,
        "legitimate_propagation_source": "detected: facts of `forbidden` artefacts the case principal can read "
                                         "elsewhere (kbsim.delivery.propagation)",
        "case_exceptions": settings.get("case_exceptions") or [],
        "inputs_sha256": inputs,
        "files_sha256": files,
        "counts": counts,
        "checks": checks,
        "licenses": {"texts": "synthetic, generated by kbsim", "third_party": []},
        "commercial_use_allowed": True,
        **({"sample_of": part_of} if part_of else {}),
    }


def propagation(cases: list[dict[str, Any]], delivery: K.Delivery, readable: Any) -> list[dict[str, Any]]:
    """Facts of `forbidden` artefacts that the case principal can legitimately read elsewhere (§3)."""
    out = []
    texts = {i: _payload_text(ln.get("payload")) for i, ln in enumerate(delivery.lines) if ln.get("payload")}
    for c in cases:
        for f in c.get("forbidden") or []:
            banned = {f["entity_id"], *(f.get("also") or []), *(f.get("also_forbidden") or [])}
            for fact in f.get("facts") or []:
                if any(p["case"] == c["id"] and p["fact"] == fact["key"] for p in out):
                    continue  # the same fact of another forbidden artefact of the case
                forms = [_ws(x) for x in fact.get("any") or [] if len(_ws(x)) >= 3]
                for i, t in texts.items():
                    ln = delivery.lines[i]
                    eid = delivery.entity_of(ln)
                    if eid in banned or not readable(c["principal"], ln):
                        continue
                    hit = next((x for x in forms if x in t), None)
                    if hit:
                        out.append({"case": c["id"], "fact": fact["key"], "form": hit, "entity_id": eid,
                                    "at": ln["at"], "principal": c["principal"]})
                        break
    return out


class Readers:
    """Who can read a timeline line (§3): mail by its acl, the rest by the container acl; admins read everything."""

    def __init__(self, sources: dict[str, Any], people: dict[str, Any]):
        self.containers = {(s["id"], k): set((v or {}).get("acl") or []) for s in sources.get("sources") or []
                           for k, v in (s.get("containers") or {}).items()}
        self.groups = {p.get("principal"): set(p.get("groups") or []) for p in people.get("people") or []}
        self.admins = set(people.get("admins") or [])

    def __call__(self, principal: str, ln: dict[str, Any]) -> bool:
        if principal in self.admins:
            return True
        who = {principal, *self.groups.get(principal, set())}
        acl = ln.get("acl")
        allowed = set(acl) if acl is not None else self.containers.get((ln["source"], ln.get("container")), set())
        return bool(who & allowed)


# ================================================================================================ build
def build(tenant: str, out: Path, *, sim_dir: Path = SIM_DIR, kb_dir: Path = KB_DIR,
          organizations: Path = ORGANIZATIONS, validate: bool = True, with_sample: bool = True) -> dict[str, Any]:
    from kbsim.audit import Audit
    from kbsim.export import (
        Exporter, build_people, build_sources, load_availability, load_cases, load_company, load_scenario, read_world,
        write_yaml,
    )
    from kbsim.tenant import TenantState

    tdir = sim_dir / "tenants" / tenant
    world = sim_dir / "out" / tenant / "world.jsonl"
    company = load_company(tdir / "company.yaml")
    scenario = load_scenario(tdir / "scenario.yaml")
    cases_model = load_cases(tdir / "cases.yaml")
    events = read_world(world)
    ids = R.Ids(company.company.tenant_id, company.tools.mail_domain)
    header = f"# GENERATED by kbsim.export from sim/tenants/{tenant}/company.yaml — do not edit by hand.\n"
    out.mkdir(parents=True, exist_ok=True)
    sources_path = out / "config" / "sources.yaml"
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_data = build_sources(company, ids, events[0].ts if events else None)
    people_data = build_people(company, ids, load_availability(tdir / "availability.yaml", company))
    write_yaml(sources_path, sources_data, header)
    write_yaml(out / "config" / "people.yaml", people_data, header)

    from kbsim.export import ExportBlocked, audit_gate

    blocked = audit_gate(tenant, sim_dir)
    if blocked:
        raise ExportBlocked(blocked)
    ex = Exporter(company, scenario, events, sources_yaml=sources_path, validate=validate,
                  out_dir=sim_dir / "out" / tenant)
    ex.run()
    lines = ex.timeline()
    write_gz_lines(out / "fixtures" / "timeline.jsonl.gz", lines)
    report = ex.report()
    failed = ex.failed | {e["seq"] for e in ex.versions.errors}
    resolved = R.resolve_cases(cases_model, scenario, ex.events, ex.touches, tenant=tenant, tz=ex.clock.tz,
                               failed=failed) if cases_model else {"cases": [], "beats": {}}

    org = next((o for o in json.loads(organizations.read_text(encoding="utf-8")) if o.get("tenant") == tenant), None) \
        if organizations.is_file() else None
    language = (org or {}).get("language") or getattr(company.company, "language", None) or "en"
    cases = contract_cases(resolved, language=language)

    delivery = K.Delivery.load(out)
    declared = [lk.gold() for lk in delivery.declared()]
    implicit, implicit_stats = implicit_links(resolved, load_judged(tdir / "implicit_links.yaml"), delivery)
    gold = declared + sorted(implicit, key=lambda g: (g["since"], g["effect"], g["cause"]))
    write_text(out / "cases.jsonl", dump_jsonl(cases))
    write_text(out / "gold_links.jsonl", dump_jsonl(gold))
    if org is not None:
        write_text(out / "dashboard.json", dump_json(org))

    audit = Audit(TenantState(tenant, sim_dir)).run()
    readers = Readers(sources_data, people_data)
    checks = {
        "export": {"failed_events": sorted(report["failed_events"]), "adapter_errors": report["adapters"]["error_count"],
                   "adapters_validated": report["adapters"]["validated"], "warnings": len(report["warnings"]),
                   "stats": {k: v for k, v in sorted(report["by_source"].items())}},
        "audit": {k: len(v) for k, v in sorted(audit.items())},
        "implicit_links": dict(sorted(implicit_stats.items())),
        "cases_skipped": [c["id"] for c in resolved["cases"] if c.get("skip")],
    }
    inputs = {name: file_sha(tdir / name) for name in ("company.yaml", "scenario.yaml", "cases.yaml", "densify.yaml",
                                                         "availability.yaml", "banned_terms.yaml", "implicit_links.yaml",
                                                         "delivery.yaml")}
    settings = yaml.safe_load((tdir / "delivery.yaml").read_text(encoding="utf-8")) if (tdir / "delivery.yaml").is_file() else {}
    inputs["world.jsonl"] = file_sha(world)
    inputs["densify/plan.json"] = file_sha(sim_dir / "out" / tenant / "densify" / "plan.json")
    inputs["organizations.json"] = file_sha(organizations)
    seed = {"densify": _densify_seed(tdir / "densify.yaml"), "engine": "texts by Nemotron Lightning; the engine "
            "itself has no randomness"}
    names = ["config/sources.yaml", "config/people.yaml", "fixtures/timeline.jsonl.gz", "cases.jsonl",
             "gold_links.jsonl"] + (["dashboard.json"] if org is not None else [])
    prop = propagation(cases, delivery, readers)
    man = manifest(tenant=tenant, name=company.company.name, language=language, inputs=inputs,
                   files={n: file_sha(out / n) for n in names}, counts=counters(delivery, gold, cases, people_data),
                   checks=checks, seed=seed, propagation=prop, settings=settings)
    write_text(out / "manifest.json", dump_json(man))

    summary = {"dir": str(out), "events": len(lines), "cases": len(cases), "gold_links": len(gold),
               "implicit": len(implicit), "files": man["files_sha256"]}
    if with_sample:
        summary["sample"] = build_sample(out, delivery, cases, gold, org=org, language=language, name=company.company.name,
                                         tenant=tenant, inputs=inputs, seed=seed, readers=readers, people=people_data,
                                         checks=checks, settings=settings)
    return summary


def _densify_seed(path: Path) -> Any:
    if not path.is_file():
        return None
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("seed")


def fixture_manifest(tenant: str, *, name: str, sim_dir: Path, sources: Path, people: Path, timeline: Path,
                     resolved: dict[str, Any] | None, report: dict[str, Any], organizations: Path = ORGANIZATIONS
                     ) -> dict[str, Any]:
    """manifest.json next to kb/fixtures/<t>/timeline.jsonl.gz (A6): the same fields as a delivery's, computed
    from the written files: what was generated from what, and how much of it."""
    from kbsim.audit import Audit
    from kbsim.tenant import TenantState

    tdir = sim_dir / "tenants" / tenant
    delivery = K.Delivery.from_files(sources, timeline)
    people_data = yaml.safe_load(people.read_text(encoding="utf-8")) or {}
    declared = [lk.gold() for lk in delivery.declared()]
    implicit, _ = implicit_links(resolved, load_judged(tdir / "implicit_links.yaml"), delivery) if resolved else ([], None)
    org = next((o for o in json.loads(organizations.read_text(encoding="utf-8")) if o.get("tenant") == tenant), None) \
        if organizations.is_file() else None
    cases = contract_cases(resolved, language=(org or {}).get("language") or "en") if resolved else []
    settings = yaml.safe_load((tdir / "delivery.yaml").read_text(encoding="utf-8")) if (tdir / "delivery.yaml").is_file() else {}
    try:
        audit_counts: dict[str, Any] = {k: len(v) for k, v in sorted(Audit(TenantState(tenant, sim_dir)).run().items())}
    except Exception as exc:  # a world that cannot be loaded still exports (its broken events are reported)
        audit_counts = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    inputs = {name: file_sha(tdir / name) for name in ("company.yaml", "scenario.yaml", "cases.yaml", "densify.yaml",
                                                         "availability.yaml", "banned_terms.yaml", "implicit_links.yaml",
                                                         "delivery.yaml")}
    inputs["world.jsonl"] = file_sha(sim_dir / "out" / tenant / "world.jsonl")
    inputs["densify/plan.json"] = file_sha(sim_dir / "out" / tenant / "densify" / "plan.json")
    return manifest(
        tenant=tenant, name=name, language=(org or {}).get("language") or "en",
        inputs=inputs, files={"timeline.jsonl.gz": file_sha(timeline), "config/sources.yaml": file_sha(sources),
                              "config/people.yaml": file_sha(people)},
        counts=counters(delivery, declared + implicit, cases, people_data),
        checks={"export": {"failed_events": sorted(report["failed_events"]),
                           "adapter_errors": report["adapters"]["error_count"], "warnings": len(report["warnings"])},
                "audit": audit_counts, "audit_thresholds": settings.get("audit_thresholds") or {}},
        seed={"densify": _densify_seed(tdir / "densify.yaml")}, propagation=[], settings=settings)


def build_sample(out: Path, delivery: K.Delivery, cases: list[dict[str, Any]], gold: list[dict[str, Any]], *,
                 org: dict[str, Any] | None, language: str, name: str, tenant: str, inputs: dict[str, Any], seed: Any,
                 readers: Readers, people: dict[str, Any], checks: dict[str, Any],
                 settings: dict[str, Any] | None = None) -> dict[str, Any]:
    ns = (org or {}).get("northStar") or {}
    label = str(ns.get("reportLabel") or "report").casefold()
    reports = {delivery.entity_of(ln) or "" for ln in delivery.lines if delivery.sources.provider(ln["source"]) == "confluence"
               and ln.get("payload") and label in {str(x.get("name") or "").casefold()
                                                   for x in ((ln["payload"].get("labels") or {}).get("results") or [])}}
    chosen, idx = sample_slice(cases, delivery, gold, always=reports)  # the weekly reports keep North Star alive
    s = out / "sample"
    (s / "config").mkdir(parents=True, exist_ok=True)
    for n in ("sources.yaml", "people.yaml"):
        (s / "config" / n).write_bytes((out / "config" / n).read_bytes())
    lines = [delivery.lines[i] for i in idx]
    write_gz_lines(s / "fixtures" / "timeline.jsonl.gz", lines)
    sd = K.Delivery.load(s)
    sgold = [g for g in gold if g["effect"] in sd.entities and g.get("cause") in sd.entities]
    write_text(s / "cases.jsonl", dump_jsonl(chosen))
    write_text(s / "gold_links.jsonl", dump_jsonl(sgold))
    if org is not None:
        write_text(s / "dashboard.json", dump_json(org))
    names = ["config/sources.yaml", "config/people.yaml", "fixtures/timeline.jsonl.gz", "cases.jsonl",
             "gold_links.jsonl"] + (["dashboard.json"] if org is not None else [])
    man = manifest(tenant=tenant, name=name, language=language, inputs=inputs,
                   files={n: file_sha(s / n) for n in names}, counts=counters(sd, sgold, chosen, people),
                   checks={"export": checks["export"], "audit": checks["audit"]}, seed=seed,
                   propagation=propagation(chosen, sd, readers), part_of="..",
                   settings={**(settings or {}), "case_exceptions": [x for x in (settings or {}).get("case_exceptions") or []
                                                                      if x.get("case") in {c["id"] for c in chosen}]})
    write_text(s / "manifest.json", dump_json(man))
    return {"events": len(lines), "cases": len(chosen), "gold_links": len(sgold),
            "categories": dict(sorted(Counter(c["category"] for c in chosen).items()))}


# ================================================================================================ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kbsim.delivery", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("split", help="assign dev/holdout to the cases of a tenant and write them into cases.yaml")
    p.add_argument("tenant")
    p.add_argument("--sim-dir", type=Path, default=SIM_DIR)
    p = sub.add_parser("build", help="build the delivery directory (same as kbsim.export <t> --delivery <dir>)")
    p.add_argument("tenant")
    p.add_argument("out", type=Path)
    p.add_argument("--sim-dir", type=Path, default=SIM_DIR)
    p.add_argument("--no-sample", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "split":
        path = args.sim_dir / "tenants" / args.tenant / "cases.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        splits = assign_splits((c["id"], c["category"]) for c in data["cases"])
        n = write_splits(path, splits)
        print(json.dumps({"cases": n, "by_split": dict(Counter(splits.values()))}, ensure_ascii=False))
        return 0
    print(json.dumps(build(args.tenant, args.out, sim_dir=args.sim_dir, with_sample=not args.no_sample),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
