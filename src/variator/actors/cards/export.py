"""The cards as a small KB delivery (``plancy-kb-corpus/1``) that is appended to the company tenant.

One ``sim`` record of type ``habit`` (KB kind Habit: how a person or an organisation usually
behaves) per card, labelled ``profile``. The record names its actor in a field the sim adapter
turns into a graph link: ``employee`` (person), ``client``, ``vendor``, ``authority`` or
``counterparty`` (organisation). Why not a Confluence page: the Confluence adapter resolves
only Jira, Confluence and GitLab URLs and people mentions, so a page cannot link a client or a
vendor; a sim record can.

The delivery is an increment of the base delivery (manifest ``increment_of``): its config is the
base config, it has no cases, and ``kbsim.intake validate`` treats it as a slice
(``sample_of``). Load it with ``python -m kb.cli replay --tenant company --fixtures <dir>``: replay
only adds deliveries (it is idempotent); it never resets the tenant.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from variator.actors.cards.artifacts import profile
from variator.orgsim.delivery import write_delivery

KB_DIR = Path(__file__).resolve().parents[4] / "kb"


def card_record(card: dict[str, Any], prof: dict[str, Any], at: str, seq: int, t: str) -> dict[str, Any]:
    """The timeline line of one card."""
    statement = " ".join(s["text"] for s in prof["sentences"])
    habits = []
    for r in card.get("habits") or []:
        if r.get("point") is None:
            continue
        value = f"{r['value']} {r['suffix']}".strip()
        habits.append(
            f"- {r['label']}: {value}"
            + (f" (range {r['interval']})" if r["interval"] else "")
            + f"; source {r['sourceLabel']}"
        )
    changes = [f"- Case {c['case']}, {c['kind']}: {c['label']}" for c in card.get("changes") or []]
    parts = [f"Habits learned from the history before {t}:", *habits]
    if changes:
        parts += ["What changes this actor:", *changes]
    evidence = list(dict.fromkeys(e["id"] for s in prof["sentences"] for e in s["cites"]))
    fields: dict[str, Any] = {
        "title": f"Profile: {card['name']}",
        "statement": statement,
        "description": "\n".join(parts),
        "labels": ["profile"],
        "card": card["id"],
        "card_kind": card["kind"],
        "as_of": t,
        "evidence": evidence,
    }
    link = card.get("link")
    if link:
        fields[link["field"]] = link["id"]
        fields[f"{link['field']}_name"] = link["name"]
    payload = {
        "run": "history",
        "option": "base",
        "scenario": "actual",
        "ts": at,
        "process": "actor_cards",
        "kind": "profile_written",
        "entity_id": card["externalId"],
        "fields": fields,
        "causes": [],
        "seq": seq,
    }
    return {
        "at": at,
        "source": "sim",
        "object_type": "habit",
        "external_id": card["externalId"],
        "container": card["container"],
        "op": "upsert",
        "payload": payload,
    }


def text_references(cards: list[dict[str, Any]], lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gold links of the text: a profile that names a person with a card refers to that card (§6)."""
    people = [c for c in cards if c["kind"] == "person"]
    out = []
    for card, ln in zip(cards, lines, strict=True):
        text = ln["payload"]["fields"]["statement"]
        for p in people:
            if p["id"] != card["id"] and p["name"] in text:
                out.append(
                    {
                        "effect": f"sim.{card['externalId']}",
                        "predicate": "refers_to",
                        "cause": f"sim.{p['externalId']}",
                        "explicit": False,
                        "expressed_as": "text",
                        "quote": p["name"],
                        "since": ln["at"],
                    }
                )
    return out


def export_delivery(
    plan: dict[str, Any],
    answers: dict[str, dict[str, Any]],
    known: dict[str, Any] | None,
    base_delivery: str | Path,
    out_dir: str | Path,
    at: str,
) -> dict[str, Any]:
    """Write the card delivery to ``out_dir`` (replaced) and return its manifest."""
    base = Path(base_delivery)
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    sources = yaml.safe_load((base / "config" / "sources.yaml").read_text(encoding="utf-8"))
    people = yaml.safe_load((base / "config" / "people.yaml").read_text(encoding="utf-8"))
    base_manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    dashboard = (
        json.loads((base / "dashboard.json").read_text(encoding="utf-8"))
        if (base / "dashboard.json").is_file()
        else None
    )
    t0 = datetime.fromisoformat(at)
    lines = []
    for i, card in enumerate(plan["cards"]):
        stamp = (t0 + timedelta(seconds=i)).isoformat()
        lines.append(
            card_record(card, profile(card, answers.get(card["id"]), known), stamp, i + 1, plan["t"])
        )
    gold = text_references(plan["cards"], lines)
    tenant = (sources.get("tenant") or {}).get("id")
    return write_delivery(
        out,
        tenant=tenant,
        sources=sources,
        people=people,
        lines=lines,
        seed=int(base_manifest.get("seed") or 0),
        name=base_manifest.get("name"),
        dashboard=dashboard,
        gold_links=gold,
        sample_of=base_manifest.get("run_id"),
        extra_manifest={
            "increment_of": base_manifest.get("run_id"),
            "increment": "actor cards (variator.actors.cards); append with kb.cli replay, never seed --reset",
        },
    )


def intake_validate(delivery: str | Path, report_dir: str | Path, kb_dir: Path = KB_DIR) -> dict[str, Any]:
    """Run ``kbsim.intake validate`` on the delivery (no stack needed); return its report."""
    env = {**os.environ, "KB_ENV": "test", "PYTHONPATH": f"{kb_dir / 'sim'}{os.pathsep}{kb_dir / 'app'}"}
    rep = Path(report_dir)
    run = subprocess.run(
        [sys.executable, "-m", "kbsim.intake", "validate", str(delivery), "--out", str(rep)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    path = rep / "validate.json"
    if not path.is_file():
        raise RuntimeError(f"intake validate wrote no report: {run.stderr[-2000:]}")
    report = json.loads(path.read_text(encoding="utf-8"))
    report["returncode"] = run.returncode
    return report
