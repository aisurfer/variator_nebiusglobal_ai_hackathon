"""The card extractor: Nemotron Ultra reads each actor's facts and records and writes its card (resumable).

Same protocol as ``variator.narrative.writers`` (no tools and no MCP), with a card schema: each sentence
lists the fact keys or record ids it cites. With the actor's records (``records``, newest first, before T)
the request asks for the actor's behaviour traits too, each citing the records it comes from. Code makes
every number of the world model; the card only says what its facts and records say. On disk under ``<work>``:

    records.json         per card: the records the requests carried (the checker reads the same ones)
    requests/<id>.json   prompt, schema and card ids of one call
    responses/<id>.json  the CLI result (structured output, cost, time, errors)
    texts.json           per card: status (accepted | failed | dropped), attempts, problems, answer

A card that fails gets a retry request with its problems; after ``max_attempts`` it is dropped
and the page shows the fact texts instead (``check.fallback``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from variator.actors.cards.check import TRAITS, check_card, citable, limits
from variator.llm.models import ROLE_MODEL
from variator.narrative.check import Names
from variator.narrative.writers import call, nemotron_request

BATCH = 4
KIND_TEXT = {
    "person": "an employee",
    "client": "a key client (modelled one by one)",
    "vendor": "a vendor",
    "regulator": "the tax authority (a rule set and a payment calendar)",
    "cohort": "a cohort (a group modelled as one actor)",
    "company": "the company itself",
}
RULES = """RULES
- 3 to 5 sentences per card, 45 to 100 words in total; a card with only 2 or 3 facts gets one sentence
  per fact (25 words or more in total). Third person, plain British business English, calm.
- Start with who the actor is. Then how it works or behaves. End with its main risk or change,
  if a fact gives one.
- Each sentence uses 1 to 3 facts of its own card and lists their keys in "cites".
- Copy every number, range, percentage and time exactly as the cited fact writes it.
  Do not round, convert, add up, compare or compute numbers. Do not write numbers as words.
- Write a date as "1 April 2025" (the same day as in the fact).
- Write flowing prose for a reader, not a list of fields. No filler such as "which matters for planning".
- Name only the people and organisations that the card's facts name. No ids, no URLs, no e-mail addresses.
- No advice, no praise or blame of people, no guesses about causes or the future, no weekdays.
- For an employee, describe the work habits only."""
RECORD_RULES = """RECORDS
- A card may list its actor's own records before the as-of date, newest first: id, date, kind and one line.
- Read them to see how the actor behaves. Extract 2 to 5 behaviour traits: each is one short sentence,
  cites 1 to 3 record ids in "cites" and says only what those records show (a pattern, not one event).
- A profile sentence may cite record ids as well as fact keys, 1 to 3 in total. Numbers come only from
  the facts or the records the sentence cites, copied exactly. Prefer the facts for numbers.
- Cite only records of the same card. No record ids in the text."""
# A worked example for the extractor (fictional actor; it passes the checker, tests/actors/test_cards.py)
EXAMPLE = {
    "card": {
        "id": "CL-EX",
        "actor": "Harbour Optics",
        "is": KIND_TEXT["client"],
        "title": "",
        "facts": [
            {
                "key": "f1",
                "text": "Harbour Optics (retail) is a key client since 2024-02-01; "
                "account manager Priya Shah.",
            },
            {"key": "f2", "text": "Pays an invoice 30-41 (median 35) days after issue on average."},
            {"key": "f3", "text": "Opens 1.2-2.6 (median 1.8) support tickets per month."},
        ],
        "records": [
            {
                "id": "sim.SUP-9001",
                "date": "2026-03-06",
                "kind": "ticket escalated",
                "text": "[Harbour Optics] Export to CSV fails; status escalated; priority P2; category bug",
            },
            {
                "id": "sim.SUP-9001",
                "date": "2026-03-04",
                "kind": "ticket opened",
                "text": "[Harbour Optics] Export to CSV fails; status open; priority P2; category bug",
            },
            {
                "id": "sim.SUP-8950",
                "date": "2026-01-19",
                "kind": "ticket escalated",
                "text": "[Harbour Optics] Report totals differ; status escalated; priority P2; category bug",
            },
            {
                "id": "sim.PAY-9002",
                "date": "2026-02-28",
                "kind": "payment received",
                "text": "Payment PAY-9002: INV-9001; status completed; amount 18400; currency USD",
            },
        ],
    },
    "answer": {
        "sentences": [
            {
                "text": "Harbour Optics has been a key retail client since 1 February 2024, with Priya Shah "
                "as its account manager.",
                "cites": ["f1"],
            },
            {"text": "It pays an invoice 30-41 (median 35) days after issue on average.", "cites": ["f2"]},
            {
                "text": "It opens 1.2-2.6 (median 1.8) support tickets per month, and it escalates the bugs "
                "that block its reports.",
                "cites": ["f3", "sim.SUP-9001", "sim.SUP-8950"],
            },
        ],
        "traits": [
            {
                "text": "Escalates a bug that blocks its reporting within days.",
                "cites": ["sim.SUP-9001", "sim.SUP-8950"],
            },
            {"text": "Settles its invoices in full by a single payment.", "cites": ["sim.PAY-9002"]},
        ],
    },
}


def wkey(card_id: str) -> str:
    """The JSON key of a card in the writer schema (the API allows [a-zA-Z0-9_.-] only)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", card_id)


def card_for_writer(
    card: dict[str, Any], known: dict[str, Any] | None, records: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    out = {
        "id": wkey(card["id"]),
        "actor": card["name"],
        "is": KIND_TEXT.get(card["kind"], card["kind"]),
        "title": card.get("title") or "",
        "facts": [{"key": f["key"], "text": f["text"]} for f in citable(card, known)],
    }
    if records:
        out["records"] = [{k: r[k] for k in ("id", "date", "kind", "text")} for r in records]
    return out


def _record_ids(records: list[dict[str, Any]] | None) -> list[str]:
    return list(dict.fromkeys(r["id"] for r in records or []))


def schema_for(
    cards: list[dict[str, Any]],
    known: dict[str, Any] | None,
    records: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    props = {}
    for c in cards:
        rids = _record_ids((records or {}).get(c["id"]))
        keys = [f["key"] for f in citable(c, known)] + rids
        props[wkey(c["id"])] = {
            "type": "object",
            "properties": {
                "sentences": {
                    "type": "array",
                    "minItems": limits(len(keys))[0],
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "cites": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": {"type": "string", "enum": keys},
                            },
                        },
                        "required": ["text", "cites"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["sentences"],
            "additionalProperties": False,
        }
        if rids:
            props[wkey(c["id"])]["properties"]["traits"] = {
                "type": "array",
                "minItems": min(TRAITS[0], len(rids)),
                "maxItems": TRAITS[1],
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "cites": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "items": {"type": "string", "enum": rids},
                        },
                    },
                    "required": ["text", "cites"],
                    "additionalProperties": False,
                },
            }
            props[wkey(c["id"])]["required"] = ["sentences", "traits"]
    return {
        "type": "object",
        "properties": props,
        "required": [wkey(c["id"]) for c in cards],
        "additionalProperties": False,
    }


def prompt_for(
    company: str,
    cards: list[dict[str, Any]],
    known: dict[str, Any] | None,
    retry: dict[str, dict[str, Any]] | None = None,
    records: dict[str, list[dict[str, Any]]] | None = None,
    examples: list[dict[str, Any]] | None = None,
) -> str:
    recs = records or {}
    if any(recs.get(c["id"]) for c in cards):
        parts = [
            f"Extract the card of each actor below from its facts and its records. The company is {company}, "
            "a UK IT services company. Together the cards are the company's world model: its CEO reads them "
            "to see who each actor is and how it usually behaves.",
            RULES,
            RECORD_RULES,
            'OUTPUT\nOne JSON object: one key per card id; its value is {"sentences": [{"text": "...", '
            '"cites": ["f2", "sim.SUP-0001"]}], "traits": [{"text": "...", "cites": ["sim.SUP-0001"]}]}. '
            'A card without records has no "traits".',
            "EXAMPLES\n" + json.dumps([EXAMPLE, *(examples or [])], ensure_ascii=False, indent=1),
        ]
    else:
        parts = [
            f"Write the short profile of each actor card below. The company is {company}, a UK IT services "
            "company. Its CEO reads the cards to see who each actor is and how it usually behaves.",
            RULES,
            'OUTPUT\nOne JSON object: one key per card id; its value is {"sentences": [{"text": "...", '
            '"cites": ["f2"]}]}.',
        ]
    if retry:
        fixes = [
            {
                "id": wkey(cid),
                "problems_of_your_previous_profile": r["problems"],
                "previous_profile": r["answer"],
            }
            for cid, r in retry.items()
        ]
        parts.append(
            "YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix every problem; keep what was right.\n"
            + json.dumps(fixes, ensure_ascii=False, indent=1)
        )
    parts.append(
        "CARDS\n"
        + json.dumps(
            [card_for_writer(c, known, recs.get(c["id"])) for c in cards], ensure_ascii=False, indent=1
        )
    )
    return "\n\n".join(parts)


def request_id(round_no: int, ids: list[str]) -> str:
    return f"r{round_no}-" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:10]


class CardWriters:
    def __init__(
        self,
        plan: dict[str, Any],
        work: str | Path,
        names: Names,
        known: dict[str, Any] | None,
        *,
        parallel: int = 4,
        timeout: int = 300,
        max_attempts: int = 3,
        model: str = ROLE_MODEL["actor_cards"],
        runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        log: Callable[[str], None] = print,
        records: dict[str, list[dict[str, Any]]] | None = None,
        reasoning: bool = True,
    ):
        self.plan = plan
        self.cards = {c["id"]: c for c in plan["cards"]}
        self.work = Path(work)
        self.names, self.known = names, known
        self.t = date.fromisoformat(plan["t"])
        self.company = (plan.get("company") or {}).get("name") or "the company"
        self.parallel, self.timeout, self.max_attempts, self.model = parallel, timeout, max_attempts, model
        self.records, self.reasoning = records or {}, reasoning
        self.runner = runner or (lambda request: call(request, self.work / "cwd", self.timeout))
        self.log = log
        for d in ("requests", "responses", "cwd"):
            (self.work / d).mkdir(parents=True, exist_ok=True)

    def check(self, cid: str, answer: dict[str, Any] | None) -> list[str]:
        return check_card(
            self.cards[cid],
            answer,
            self.names,
            t=self.t,
            known=self.known,
            company=self.company,
            records=self.records.get(cid),
        )

    def examples(self, st: dict[str, Any], skip: list[str], n: int = 2) -> list[dict[str, Any]]:
        """Up to ``n`` accepted cards of earlier calls, facts and answer (the style stays the same)."""
        out = []
        for cid, s in st.items():
            if len(out) >= n:
                break
            if s.get("status") == "accepted" and cid not in skip and cid in self.cards:
                ans = {k: v for k, v in s["answer"].items() if k in ("sentences", "traits")}
                card = card_for_writer(self.cards[cid], self.known, self.records.get(cid))
                out.append({"card": card, "answer": ans})
        return out

    def load_state(self) -> dict[str, Any]:
        p = self.work / "texts.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    def save_state(self, st: dict[str, Any]) -> None:
        (self.work / "texts.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )

    def run(self, ids: list[str] | None = None) -> dict[str, Any]:
        wanted = [c for c in (ids or list(self.cards)) if citable(self.cards[c], self.known)]
        st = self.load_state()
        for c in wanted:  # facts changed since the answer was accepted: write the card again
            if st.get(c, {}).get("status") == "accepted" and self.check(c, st[c].get("answer")):
                self.log(f"  {c}: the stored answer fails the check now; written again")
                st.pop(c)
        for rnd in range(self.max_attempts):
            todo = [
                c
                for c in wanted
                if st.get(c, {}).get("status") not in ("accepted", "dropped")
                and len(st.get(c, {}).get("attempts", [])) == rnd
            ]
            if not todo:
                continue
            reqs = []
            for i in range(0, len(todo), BATCH):
                group = todo[i : i + BATCH]
                rid = request_id(rnd, group)
                retry = {c: st[c]["attempts"][-1] for c in group if rnd > 0}
                cards = [self.cards[c] for c in group]
                req = {
                    "id": rid,
                    "round": rnd,
                    "cards": group,
                    "schema": schema_for(cards, self.known, self.records),
                    "prompt": prompt_for(
                        self.company,
                        cards,
                        self.known,
                        retry or None,
                        self.records,
                        self.examples(st, group) if any(self.records.get(c) for c in group) else None,
                    ),
                }
                rp = self.work / "requests" / f"{rid}.json"
                if not rp.is_file():
                    rp.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
                reqs.append(req)
            missing = [r for r in reqs if not (self.work / "responses" / f"{r['id']}.json").is_file()]
            self.log(f"round {rnd}: {len(todo)} cards in {len(reqs)} requests, {len(missing)} to call")
            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                for r, res in zip(missing, pool.map(self._call, missing), strict=True):
                    (self.work / "responses" / f"{r['id']}.json").write_text(
                        json.dumps({"request": r["id"], **res}, ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                    self.log(
                        f"  {r['id']}: ok={res.get('ok')} {res.get('seconds')} s"
                        + (f" {res.get('error')}" if res.get("error") else "")
                    )
            for r in reqs:
                res = json.loads((self.work / "responses" / f"{r['id']}.json").read_text(encoding="utf-8"))
                for c in r["cards"]:
                    answer = (res.get("structured") or {}).get(wkey(c)) if res.get("ok") else None
                    probs = (
                        self.check(c, answer)
                        if answer
                        else [f"no answer ({res.get('error') or 'call failed'})"]
                    )
                    s = st.setdefault(c, {"attempts": []})
                    if any(a.get("request") == r["id"] for a in s["attempts"]):
                        continue
                    s["attempts"].append({"request": r["id"], "problems": probs, "answer": answer or {}})
                    if not probs:
                        s["status"], s["answer"] = "accepted", answer
                    else:
                        s["status"] = "dropped" if len(s["attempts"]) >= self.max_attempts else "failed"
            self.save_state(st)
        return self.summary(st, wanted)

    def _call(self, req: dict[str, Any]) -> dict[str, Any]:
        return self.runner(
            {**nemotron_request(req["prompt"], req["schema"], self.model), "reasoning": self.reasoning}
        )

    def summary(self, st: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
        acc = [c for c in wanted if st.get(c, {}).get("status") == "accepted"]
        drop = [c for c in wanted if st.get(c, {}).get("status") == "dropped"]
        costs, secs, models = 0.0, 0.0, []
        for p in (self.work / "responses").glob("*.json"):
            r = json.loads(p.read_text(encoding="utf-8"))
            costs += r.get("cost_usd") or 0.0
            secs += r.get("seconds") or 0.0
            if r.get("model") and r["model"] not in models:
                models.append(r["model"])
        return {
            "cards": len(wanted),
            "accepted": len(acc),
            "accepted_first_try": sum(1 for c in acc if len(st[c]["attempts"]) == 1),
            "retried": sum(1 for c in wanted if len(st.get(c, {}).get("attempts", [])) > 1),
            "rejected_answers": sum(
                1 for c in wanted for a in st.get(c, {}).get("attempts", []) if a["problems"]
            ),
            "dropped": drop,
            "requests": len(list((self.work / "responses").glob("*.json"))),
            "cost_usd": round(costs, 4),
            "call_seconds_total": round(secs, 1),
            "model": ", ".join(sorted(models)) or self.model,  # the model id the API answered with
            "records": sum(len(self.records.get(c, [])) for c in wanted),
        }

    def accepted(self) -> dict[str, dict[str, Any]]:
        """Card id -> accepted answer, re-checked now (a stored answer that fails today does not count)."""
        out = {}
        for c, s in self.load_state().items():
            if c in self.cards and s.get("status") == "accepted" and not self.check(c, s.get("answer")):
                out[c] = s["answer"]
        return out
