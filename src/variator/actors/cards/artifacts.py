"""The artifact set of the actor cards, served by ``variator.web`` (``/api/variator/artifacts/<set>/<file>``).

Files: ``manifest.json`` (``kind: actor-cards``), ``cards.json`` (every card with its profile and
citations), ``risks.json`` (the register of the risks that already fired, with the case items
that model them), ``writer.json`` (the writer and checker summary).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from variator.actors.cards import KIND, SCHEMA
from variator.actors.cards.check import citable, fallback
from variator.actors.cards.kbids import short

GROUPS = [
    {"id": "company", "label": "Company"},
    {"id": "people", "label": "People"},
    {"id": "clients", "label": "Key clients"},
    {"id": "vendors", "label": "Vendors"},
    {"id": "cohorts", "label": "Cohorts"},
]
CARD_FIELDS = (
    "id",
    "kind",
    "group",
    "section",
    "name",
    "title",
    "subtitle",
    "dossier",
    "contract",
    "kbId",
    "habits",
    "changes",
    "risks",
    "members",
    "external",
)


def dumps(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def _cite(eid: str, known: dict[str, Any] | None) -> dict[str, str]:
    k = known or {}
    return {"id": eid, "label": short(eid, k), "title": str((k.get(eid) or {}).get("title") or eid)}


def profile(
    card: dict[str, Any], answer: dict[str, Any] | None, known: dict[str, Any] | None
) -> dict[str, Any]:
    """Sentences with their citation chips (the KB records behind the cited facts, or the cited records
    themselves, at most 3 per sentence), and the behaviour traits the extractor drew from the records."""
    facts = {f["key"]: f for f in citable(card, known)}
    source = "writer" if answer else "facts"
    ans = answer or fallback(card, known)

    def chips(cites: list[str]) -> list[dict[str, str]]:
        ids: list[str] = []
        for key in cites:
            for e in facts[key].get("evidence", [])[:2] if key in facts else [key]:
                if e not in ids:
                    ids.append(e)
        return [_cite(e, known) for e in ids[:3]]

    out = [
        {"text": s["text"].strip(), "facts": list(s.get("cites") or []), "cites": chips(s.get("cites") or [])}
        for s in ans.get("sentences") or []
    ]
    traits = [
        {"text": t["text"].strip(), "cites": chips(t.get("cites") or [])} for t in ans.get("traits") or []
    ]
    return {"source": source, "sentences": out, **({"traits": traits} if traits else {})}


def ui_card(
    card: dict[str, Any], answer: dict[str, Any] | None, known: dict[str, Any] | None
) -> dict[str, Any]:
    c = {k: card[k] for k in CARD_FIELDS if k in card}
    c["profile"] = profile(card, answer, known)
    c["events"] = [
        {
            "text": e["text"],
            "date": e.get("date"),
            "cites": [_cite(x, known) for x in e.get("evidence") or [] if known is None or x in known][:2],
        }
        for e in card.get("events") or []
    ]
    return c


def ui_risks(plan: dict[str, Any], known: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for r in plan["risks"]:
        x = dict(r)
        x["examples"] = [_cite(e, known) for e in r["examples"] if known is None or e in known][:5]
        x["external"] = list(r.get("external") or [])[:5]
        out.append(x)
    return out


def _file(out: Path, rel: str, doc: Any) -> dict[str, Any]:
    text = dumps(doc)
    (out / rel).write_text(text, encoding="utf-8")
    data = text.encode("utf-8")
    return {"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_set(
    plan: dict[str, Any],
    answers: dict[str, dict[str, Any]],
    known: dict[str, Any] | None,
    out_dir: str | Path,
    writer_summary: dict[str, Any] | None = None,
    built_at: str | None = None,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cards = [ui_card(c, answers.get(c["id"]), known) for c in plan["cards"]]
    risks = ui_risks(plan, known)
    built = built_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    head = {
        "schema": SCHEMA,
        "asOf": plan["asOf"],
        "t": plan["t"],
        "company": plan["company"],
        "research": plan.get("source", {}).get("research"),
    }
    files = [
        _file(out, "cards.json", {**head, "groups": GROUPS, "cards": cards}),
        _file(out, "risks.json", {**head, "risks": risks}),
        _file(
            out,
            "writer.json",
            {
                **head,
                "summary": writer_summary or {},
                "profiles": {c["id"]: c["profile"]["source"] for c in cards},
            },
        ),
    ]
    counts: dict[str, int] = {}
    for c in cards:
        counts[c["group"]] = counts.get(c["group"], 0) + 1
    manifest = {
        "id": KIND,
        "kind": KIND,
        "schema": 1,
        "contract": SCHEMA,
        "title": "Actor cards and the register of risks that already fired",
        "asOf": plan["asOf"],
        "t": plan["t"],
        "builtAt": built,
        "producer": "variator.actors.cards (python -m variator.actors.cards.cli)",
        "source": plan.get("source") or {},
        "counts": {
            "cards": len(cards),
            "by_group": counts,
            "risks": len(risks),
            "profiles_written": sum(1 for c in cards if c["profile"]["source"] == "writer"),
            "profiles_from_facts": sum(1 for c in cards if c["profile"]["source"] == "facts"),
        },
        "writer": writer_summary or {},
        "files": files,
    }
    (out / "manifest.json").write_text(dumps(manifest), encoding="utf-8")
    return manifest
