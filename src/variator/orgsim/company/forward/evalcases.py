"""Gold links and evaluation cases of a branch delivery (corpus contract §6 and §8), made from its lines.

- Gold links: every declared cause of a ``sim`` line (``expressed_as: sim_cause``) and the text
  references of the leadership reviews and post-incident reviews (``explicit: false``, ``text``, with
  the quoted id).
- Cases: the 13 categories of §8.2 over the branch records. Each answer fact is a string of the
  evidence payload, so ``kbsim.intake validate`` can check it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from variator.orgsim.events import CAUSE_PREDICATES

# intake minimums (§8.2) x 1.15, rounded up (as orgsim.company.cases.MINIMUM)
MINIMUM = {
    "lookup": 11,
    "why": 13,
    "history": 12,
    "as_of": 10,
    "conflict": 7,
    "stale": 7,
    "impact": 4,
    "multi_hop": 7,
    "acl": 10,
    "negative": 7,
    "implicit_ref": 12,
    "numeric": 12,
    "people": 9,
}
NEGATIVE = [
    ("Did we sign a contract with Quillfeather Aerospace?", "Quillfeather Aerospace"),
    ("When did we open the office in Reykjavik?", "Reykjavik"),
    ("Which client uses our blockchain ledger module?", "blockchain ledger"),
    ("Who approved the purchase of the corporate jet?", "corporate jet"),
    ("What did the Zanzibar data centre migration cost?", "Zanzibar"),
    ("How many robots did the warehouse automation pilot use?", "warehouse automation"),
    ("Which engineer ran the quantum annealer benchmark?", "quantum annealer"),
    ("What was decided about the Antarctic research client?", "Antarctic"),
]
REF = re.compile(r"\b(?:INC-\d{4}|v\d+\.\d+\.\d+)\b")


def _text(x: Any) -> str:
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for y in v.values():
                walk(y)
        elif isinstance(v, list):
            for y in v:
                walk(y)

    walk(x)
    return " ".join(" ".join(parts).split()).casefold()


def _spread(items: list[Any], n: int) -> list[Any]:
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(k * step)] for k in range(n)]


def gold_links(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Declared causes as gold links, and the implicit text references of the review records."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    versions: dict[str, str] = {}  # deploy version -> deploy entity
    for ln in lines:
        if ln["source"] == "sim" and ln["object_type"] == "deploy":
            versions.setdefault(ln["payload"]["fields"].get("version", ""), f"sim.{ln['external_id']}")
    known = {f"sim.{ln['external_id']}" for ln in lines if ln["source"] == "sim"}
    for ln in lines:
        if ln["source"] != "sim":
            continue
        p = ln["payload"]
        eff = f"sim.{ln['external_id']}"
        for c in p.get("causes") or []:
            cause = c["id"] if "." in c["id"].split("-")[0] else f"sim.{c['id']}"
            key = (eff, CAUSE_PREDICATES[c["type"]], cause)
            if key not in out:
                out[key] = {
                    "cause": cause,
                    "effect": eff,
                    "explicit": True,
                    "expressed_as": "sim_cause",
                    "predicate": key[1],
                    "since": ln["at"],
                }
        f = p.get("fields") or {}
        if ln["object_type"] == "decision" and str(f.get("title", "")).startswith(
            ("Leadership review", "Post-incident")
        ):
            declared = {f"sim.{c['id']}" for c in p.get("causes") or []}
            for m in REF.finditer(str(f.get("statement", ""))):
                q = m.group(0)
                target = versions.get(q) if q.startswith("v") else f"sim.{q}"
                if not target or target not in known or target in declared or target == eff:
                    continue
                key = (eff, "refers_to", target)
                if key not in out:
                    out[key] = {
                        "cause": target,
                        "effect": eff,
                        "explicit": False,
                        "expressed_as": "text",
                        "predicate": "refers_to",
                        "quote": q,
                        "since": ln["at"],
                    }
    return sorted(out.values(), key=lambda g: (g["since"], g["effect"], g["predicate"], g["cause"]))


class BranchCases:
    def __init__(
        self, lines: list[dict[str, Any]], gold: list[dict[str, Any]], people: dict[str, Any], prefix: str
    ):
        self.lines, self.gold, self.prefix = lines, gold, prefix
        self.by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ln in lines:
            if ln["source"] == "sim":
                self.by_entity[f"sim.{ln['external_id']}"].append(ln)
        self.people = people
        self.cases: list[dict[str, Any]] = []

    def sim(self, otype: str) -> list[tuple[str, list[dict[str, Any]]]]:
        return sorted((e, lns) for e, lns in self.by_entity.items() if lns[0]["object_type"] == otype)

    def f(self, eid: str, k: int = -1) -> dict[str, Any]:
        return self.by_entity[eid][k]["payload"]["fields"]

    def count(self, cat: str) -> int:
        return sum(1 for c in self.cases if c["category"] == cat)

    def add(
        self,
        category: str,
        question: str,
        evidence: list[tuple[str, str]],
        facts: list[tuple[str, str]],
        *,
        difficulty: str = "easy",
        roots: list[tuple[str, str]] = (),
        principal: str = "user:admin",
        known_at: str | None = None,
        forbidden: list[dict[str, Any]] = (),
        not_found: bool = False,
        rationale: str = "",
    ) -> None:
        if self.count(category) >= MINIMUM[category]:
            return
        for eid, _ in list(evidence) + list(roots):
            if eid not in self.by_entity:
                return
        text = " ".join(_text(ln.get("payload")) for eid, _ in evidence for ln in self.by_entity[eid])
        if any(v.casefold() not in text for _, v in facts):
            return
        n = len(self.cases) + 1
        self.cases.append(
            {
                "id": f"{self.prefix}{n:03d}",
                "category": category,
                "difficulty": difficulty,
                "language": "en",
                "split": "holdout" if n % 10 in (3, 6, 9) else "dev",
                "question": question,
                "principal": principal,
                "known_at": known_at,
                "evidence": [{"entity_id": e, "why": w} for e, w in evidence],
                "roots": [{"entity_id": e, "why": w} for e, w in roots],
                "forbidden": list(forbidden),
                "answer_facts": [{"key": k, "any": [v]} for k, v in facts],
                "expect_not_found": not_found,
                "rationale": rationale,
            }
        )

    # ------------------------------------------------------------------ categories
    def build(self) -> list[dict[str, Any]]:
        for step in (
            self.lookup,
            self.numeric,
            self.why,
            self.history,
            self.as_of,
            self.conflict,
            self.stale,
            self.impact,
            self.multi_hop,
            self.acl,
            self.negative,
            self.implicit_ref,
            self.people_cases,
        ):
            step()
        return self.cases

    def lookup(self) -> None:
        rows = [
            (e, lns)
            for e, lns in self.sim("contract")
            if lns[-1]["payload"]["fields"].get("owner_name")
            and lns[-1]["payload"]["fields"].get("client_name")
            and lns[-1]["payload"]["fields"].get("contract_type") != "vendor"
        ]
        for eid, lns in _spread(rows, MINIMUM["lookup"] + 3):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "lookup",
                f"Who is the account manager for {f['client_name']}?",
                [(eid, "the contract names the account manager")],
                [("account_manager", f["owner_name"])],
                rationale="The owner of the client contract.",
            )

    def numeric(self) -> None:
        rows = [
            (e, lns)
            for e, lns in self.sim("invoice")
            if lns[0]["payload"]["kind"] == "invoice_issued" and lns[-1]["payload"]["fields"].get("period")
        ]
        for eid, lns in _spread(rows, MINIMUM["numeric"] + 3):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "numeric",
                f"Which billing period does invoice {f['number']} to {f.get('client_name', '')} cover?",
                [(eid, "the invoice")],
                [("period", f["period"])],
                difficulty="medium",
                rationale="The billing period of the invoice.",
            )

    def why(self) -> None:
        les = dict(self.sim("life_event"))
        for eid, lns in self.sim("contract"):
            notice = [ln for ln in lns if ln["payload"]["kind"] == "notice_given"]
            causes = [c["id"] for ln in notice for c in ln["payload"]["causes"] if c["type"] == "life_event"]
            if not notice or not causes or f"sim.{causes[0]}" not in les:
                continue
            le = f"sim.{causes[0]}"
            reason = les[le][-1]["payload"]["fields"]["statement"].split(": ", 1)[-1].rstrip(".")
            client = notice[-1]["payload"]["fields"].get("client_name", "")
            self.add(
                "why",
                f"Why did {client} give notice on its contract?",
                [(eid, "the contract with the notice"), (le, "the client's decision")],
                [("reason", reason)],
                roots=[(le, "the life event of the client")],
                difficulty="medium",
                rationale="The notice is caused by the client's life event.",
            )
        for eid, lns in self.sim("incident"):
            f = lns[-1]["payload"]["fields"]
            cause = next((c for c in lns[-1]["payload"]["causes"]), None)
            if cause is None or f"sim.{cause['id']}" not in self.by_entity:
                continue
            ce = f"sim.{cause['id']}"
            cf = self.f(ce)
            fact = cf.get("version") or cf.get("title")
            if not fact:
                continue
            self.add(
                "why",
                f"What caused the incident '{f['title']}' that started at {f['started_at'][:16]}?",
                [(eid, "the incident"), (ce, "its declared cause")],
                [("cause", fact)],
                roots=[(ce, "the cause")],
                difficulty="hard",
                rationale="The incident names its cause.",
            )
        for eid, lns in _spread(self.sim("payment"), MINIMUM["why"]):
            f = lns[-1]["payload"]["fields"]
            inv = f.get("settles")
            if not inv or f"sim.{inv}" not in self.by_entity:
                continue
            self.add(
                "why",
                f"Why was payment {f['number']} made?",
                [(eid, "the payment"), (f"sim.{inv}", "the invoice")],
                [("invoice", inv)],
                roots=[(f"sim.{inv}", "the invoice it settles")],
                difficulty="medium",
                rationale="A payment settles an invoice.",
            )

    def history(self) -> None:
        rows = [
            (e, lns)
            for e, lns in self.sim("invoice")
            if lns[0]["payload"]["kind"] == "invoice_issued"
            and lns[-1]["payload"]["fields"].get("status") == "paid"
        ]
        for eid, lns in _spread(rows, MINIMUM["history"] + 3):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "history",
                f"Which status did invoice {f['number']} have before it was paid?",
                [(eid, "the invoice and its versions")],
                [("status", "issued")],
                difficulty="medium",
                rationale="The invoice went from issued to paid.",
            )

    def as_of(self) -> None:
        rows = [
            (e, lns)
            for e, lns in self.sim("invoice")
            if lns[0]["payload"]["kind"] == "invoice_issued" and len(lns) >= 2
        ]
        deals = [(e, lns) for e, lns in self.sim("deal") if len(lns) >= 3]
        for eid, lns in _spread(deals, 6):
            ln, nxt = lns[1], lns[2]
            f = ln["payload"]["fields"]
            if f["status"] == nxt["payload"]["fields"]["status"]:
                continue
            self.add(
                "as_of",
                f"On {ln['at'][:10]}, which stage was the deal '{f['title']}' in?",
                [(eid, "the deal and its stages")],
                [("stage", f["status"])],
                known_at=nxt["at"],
                difficulty="hard",
                rationale="The stage of the deal before its next change.",
            )
        for eid, lns in _spread(rows, MINIMUM["as_of"] + 3):
            f = lns[0]["payload"]["fields"]
            self.add(
                "as_of",
                f"On {lns[0]['at'][:10]}, was invoice {f['number']} paid?",
                [(eid, "the invoice")],
                [("status", "issued")],
                known_at=lns[1]["at"],
                difficulty="hard",
                rationale="On the issue date the invoice was issued, not paid.",
            )

    def conflict(self) -> None:
        for eid, lns in _spread(self.sim("incident"), MINIMUM["conflict"] + 3):
            f = lns[-1]["payload"]["fields"]
            if f.get("status") != "resolved":
                continue
            self.add(
                "conflict",
                f"The incident '{f['title']}' shows two statuses. Which one is current?",
                [(eid, "the incident and its versions")],
                [("status", "resolved")],
                difficulty="medium",
                rationale="The newest version of the incident is current.",
            )

    def stale(self) -> None:
        rows = [(e, lns) for e, lns in self.sim("contract") if len(lns) >= 2]
        for eid, lns in _spread(rows, MINIMUM["stale"] + 3):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "stale",
                f"What is the current status of the contract of {f.get('client_name', '')}?",
                [(eid, "the contract and its changes")],
                [("status", f["status"])],
                difficulty="medium",
                rationale="Only the newest version of the contract is current.",
            )
        for eid, lns in _spread(
            [(e, x) for e, x in self.sim("employee_event") if len(x) >= 2], MINIMUM["stale"]
        ):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "stale",
                f"What is the status of the {f.get('type', 'leave').replace('_', ' ')} record of "
                f"{f.get('employee_name', '')} from {f.get('effective_date', '')}?",
                [(eid, "the employee record")],
                [("status", f["status"])],
                difficulty="medium",
                rationale="The newest version of the record is current.",
            )

    def impact(self) -> None:
        rows = [
            (e, lns) for e, lns in self.sim("incident") if lns[-1]["payload"]["fields"].get("client_name")
        ]
        rows.sort(key=lambda x: x[1][-1]["payload"]["fields"].get("severity", "SEV9"))
        for eid, lns in rows[: MINIMUM["impact"] + 3]:
            f = lns[-1]["payload"]["fields"]
            self.add(
                "impact",
                f"Which client was the largest one affected by the incident '{f['title']}' on "
                f"{f['started_at'][:10]}?",
                [(eid, "the incident")],
                [("client", f["client_name"])],
                difficulty="medium",
                rationale="The primary affected client of the incident.",
            )

    def multi_hop(self) -> None:
        rows = []
        for eid, lns in self.sim("payment"):
            f = lns[-1]["payload"]["fields"]
            inv = f"sim.{f.get('settles')}"
            if inv in self.by_entity:
                ctr = self.f(inv).get("contract")
                if ctr and f"sim.{ctr}" in self.by_entity:
                    rows.append((eid, inv, f"sim.{ctr}", f["number"], ctr))
        for pay, inv, ctr, num, cnum in _spread(rows, MINIMUM["multi_hop"] + 3):
            self.add(
                "multi_hop",
                f"Under which contract was the invoice that payment {num} settles issued?",
                [(pay, "the payment"), (inv, "the invoice it settles"), (ctr, "the contract of the invoice")],
                [("contract", cnum)],
                difficulty="hard",
                rationale="Payment -> invoice (settles) -> contract.",
            )

    def acl(self) -> None:
        engineers = sorted(
            p["principal"].split(":", 1)[1]
            for p in self.people.get("people", [])
            if p.get("department") == "Engineering" and not p.get("end_date")
        )
        if "sim.PROC-PAYROLL" not in self.by_entity or not engineers:
            return
        rows = [e for e, _ in self.sim("payroll")] + [e for e, _ in self.sim("tax_payment")]
        for k, eid in enumerate(rows):
            title = self.f(eid).get("title")
            if not title:
                continue
            self.add(
                "acl",
                f"What was the total of the {title.lower()}?",
                [("sim.PROC-PAYROLL", "the payroll process")],
                [("pay_day", "25th")],
                principal=f"user:{engineers[k % len(engineers)]}",
                difficulty="hard",
                forbidden=[
                    {
                        "entity_id": eid,
                        "why": "payroll and taxes are visible to Finance and Management only",
                        "facts": [{"key": "title", "any": [title]}],
                    }
                ],
                rationale="An engineer sees the payroll process but not the amounts.",
            )

    def negative(self) -> None:
        all_text = " ".join(_text(ln.get("payload")) for ln in self.lines)
        for q, kw in NEGATIVE:
            if kw.casefold() in all_text:
                continue
            self.add(
                "negative",
                q,
                [],
                [],
                not_found=True,
                difficulty="medium",
                rationale=f"Nothing in the branch mentions it. Keywords: {kw}",
            )

    def implicit_ref(self) -> None:
        rows = [g for g in self.gold if not g["explicit"]]
        for g in _spread(rows, MINIMUM["implicit_ref"] + 3):
            title = self.f(g["effect"]).get("title", "")
            self.add(
                "implicit_ref",
                f"Which record does '{title}' mean by \"{g['quote']}\"?",
                [(g["effect"], "the review"), (g["cause"], "the record it names")],
                [("quote", g["quote"])],
                difficulty="hard",
                rationale="The review names the record in its text, without a declared link.",
            )

    def people_cases(self) -> None:
        for eid, lns in _spread(self.sim("incident"), MINIMUM["people"] + 3):
            f = lns[-1]["payload"]["fields"]
            if not f.get("assignee_name"):
                continue
            self.add(
                "people",
                f"Who handled the incident '{f['title']}' on {f['started_at'][:10]}?",
                [(eid, "the incident")],
                [("assignee", f["assignee_name"])],
                rationale="The assignee of the incident.",
            )


def build(
    lines: list[dict[str, Any]], people: dict[str, Any], prefix: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gold = gold_links(lines)
    return gold, BranchCases(lines, gold, people, prefix).build()
