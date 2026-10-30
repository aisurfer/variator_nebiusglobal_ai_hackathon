"""Evaluation cases of the history delivery (corpus contract §8), made by code from the delivered lines.

Each case names its evidence by knowledge-base entity id, and each answer fact is a string
of the evidence payload, so ``kbsim.intake validate`` can check it. The categories and the
counts follow the minimums of §8.2 (about 110 cases). The texts are short templates; writer
agents can rephrase the questions later.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from variator.orgsim.company.model import World

# intake minimums (§8.2) x 1.15, rounded up: the minimums grow with the number of cases (x total / 100),
# so each category gets a little more than its minimum and none of them more than this target
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


def _spread(items: list[Any], n: int) -> list[Any]:
    """``n`` items spread evenly over a list (all of them when the list is short)."""
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(k * step)] for k in range(n)]


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


class CaseBuilder:
    def __init__(self, w: World, lines: list[dict[str, Any]], implicit: list[dict[str, Any]], slug: str):
        self.w, self.lines, self.implicit, self.slug = w, lines, implicit, slug
        self.by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ln in lines:
            self.by_entity[self.entity(ln)].append(ln)
        self.cases: list[dict[str, Any]] = []
        self.all_text = None

    def entity(self, ln: dict[str, Any]) -> str:
        src, ext, p = ln["source"], ln["external_id"], ln.get("payload") or {}
        if src == "sim":
            return f"sim.{ext}"
        if src.startswith("jira"):
            return f"jira.{ext}"
        if src.startswith("gitlab"):
            return f"gitlab.{self.slug}.mr-{p.get('iid')}"
        if src.startswith("mail"):
            return f"mail.{p.get('id')}"
        return f"confluence.page-{ext}"

    def sim(self, otype: str) -> list[tuple[str, list[dict[str, Any]]]]:
        out = [
            (e, lns)
            for e, lns in self.by_entity.items()
            if lns[0]["source"] == "sim" and lns[0]["object_type"] == otype
        ]
        return sorted(out)

    def last(self, eid: str) -> dict[str, Any]:
        return self.by_entity[eid][-1]["payload"]

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
        evid_text = " ".join(_text(ln.get("payload")) for eid, _ in evidence for ln in self.by_entity[eid])
        if any(f.casefold() not in evid_text for _, f in facts):
            return
        n = len(self.cases) + 1
        self.cases.append(
            {
                "id": f"LW{n:03d}",
                "category": category,
                "difficulty": difficulty,
                "language": "en",
                "split": "holdout" if n % 10 in (3, 6, 9) else "dev",
                "question": question,
                "principal": principal,
                "known_at": known_at,
                "evidence": [{"entity_id": e, "why": why} for e, why in evidence],
                "roots": [{"entity_id": e, "why": why} for e, why in roots],
                "forbidden": list(forbidden),
                "answer_facts": [{"key": k, "any": [v]} for k, v in facts],
                "expect_not_found": not_found,
                "rationale": rationale,
            }
        )

    def count(self, category: str) -> int:
        return sum(1 for c in self.cases if c["category"] == category)

    # ================================================================================== categories
    def build(self) -> list[dict[str, Any]]:
        self.lookup()
        self.numeric()
        self.why()
        self.history()
        self.as_of()
        self.conflict()
        self.stale()
        self.impact()
        self.multi_hop()
        self.acl()
        self.negative()
        self.implicit_ref()
        self.people()
        return self.cases

    def lookup(self) -> None:
        contracts = [
            (e, lns)
            for e, lns in self.sim("contract")
            if (lns[-1]["payload"]["fields"].get("owner_name"))
            and lns[-1]["payload"]["fields"].get("contract_type") != "vendor"
        ]
        for eid, lns in _spread(contracts, MINIMUM["lookup"] + 2):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "lookup",
                f"Who is the account manager for {f['client_name']}?",
                [(eid, "the contract names the account manager")],
                [("account_manager", f["owner_name"])],
                rationale="The owner of the client contract.",
            )

    def numeric(self) -> None:
        pages = sorted((e, lns) for e, lns in self.by_entity.items() if e.startswith("confluence.page-"))
        labels = [
            ("ARR, $k", "ARR in thousands of dollars"),
            ("Active clients", "number of active clients"),
            ("Cash, $k", "cash in thousands of dollars"),
        ]
        for k, (eid, lns) in enumerate(_spread(pages, MINIMUM["numeric"] + 2)):
            p = lns[-1]["payload"]
            props = dict(self._props(p))
            label, what = labels[k % len(labels)]
            if label not in props:
                continue
            self.add(
                "numeric",
                f"What was the {what} in the company metrics report '{p['title']}'?",
                [(eid, "the weekly metrics report")],
                [(label, props[label])],
                difficulty="medium",
                rationale="A page property of the weekly report.",
            )

    @staticmethod
    def _props(page: dict[str, Any]) -> list[tuple[str, str]]:
        import json

        doc = json.loads(page["body"]["atlas_doc_format"]["value"])
        out = []
        for node in doc["content"]:
            if node.get("type") == "bodiedExtension":
                for row in node["content"][0]["content"]:
                    cells = row["content"]
                    key = cells[0]["content"][0]["content"][0]["text"]
                    val = cells[1]["content"][0]["content"][0]["text"]
                    out.append((key, val))
        return out

    def why(self) -> None:
        les = {e: lns for e, lns in self.sim("life_event")}
        for eid, lns in self.sim("contract"):
            if self.count("why") >= MINIMUM["why"] + 2:
                break
            causes = [c for ln in lns for c in ln["payload"]["causes"] if c["type"] == "life_event"]
            notice = [ln for ln in lns if ln["payload"]["kind"] == "notice_given"]
            if not causes or not notice:
                continue
            le = f"sim.{causes[0]['id']}"
            if le not in les:
                continue
            stmt = les[le][-1]["payload"]["fields"]["statement"]
            reason = stmt.split(": ", 1)[-1].rstrip(".")
            client = notice[-1]["payload"]["fields"]["client_name"]
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
            if self.count("why") >= MINIMUM["why"] + 2:
                break
            f = lns[-1]["payload"]["fields"]
            dep = next((c["id"] for c in lns[-1]["payload"]["causes"] if c["id"].startswith("DEP-")), None)
            if not dep or f"sim.{dep}" not in self.by_entity:
                continue
            version = self.last(f"sim.{dep}")["fields"]["version"]
            self.add(
                "why",
                f"What caused the incident '{f['title']}' that started at {f['started_at'][:16]}?",
                [(eid, "the incident"), (f"sim.{dep}", "the deploy before it")],
                [("deploy", version)],
                roots=[(f"sim.{dep}", "the deploy")],
                difficulty="hard",
                rationale="The incident is caused by a deploy.",
            )

    def history(self) -> None:
        overdue = [
            (e, lns)
            for e, lns in self.sim("invoice")
            if any(ln["payload"]["kind"] == "invoice_overdue" for ln in lns)
            and lns[-1]["payload"]["fields"].get("status") == "paid"
        ]
        for eid, lns in _spread(overdue, MINIMUM["history"] + 2):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "history",
                f"Did invoice {f['number']} to {f['client_name']} become overdue before it was paid?",
                [(eid, "the invoice and its versions")],
                [("status", "overdue")],
                difficulty="medium",
                rationale="The invoice went through the status overdue.",
            )

    def as_of(self) -> None:
        deals = [(e, lns) for e, lns in self.sim("deal") if len(lns) >= 3]
        for eid, lns in _spread(deals, MINIMUM["as_of"] + 2):
            ln = lns[1]
            known = lns[2]["at"]
            f = ln["payload"]["fields"]
            if f["status"] == lns[2]["payload"]["fields"]["status"]:
                continue
            self.add(
                "as_of",
                f"On {ln['at'][:10]}, which stage was the deal '{f['title']}' in?",
                [(eid, "the deal and its stages")],
                [("stage", f["status"])],
                known_at=known,
                difficulty="hard",
                rationale="The stage of the deal before its next change.",
            )

    def conflict(self) -> None:
        mails = {}
        for e, lns in self.by_entity.items():
            if e.startswith("mail."):
                subj = lns[0]["payload"]["subject"]
                if subj.startswith("Invoice INV-"):
                    mails[subj.split()[1]] = e
        invs = [(e, lns) for e, lns in self.sim("invoice") if e.removeprefix("sim.") in mails]
        for eid, lns in _spread(invs, MINIMUM["conflict"] + 2):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "conflict",
                f"The e-mail with invoice {f['number']} and the finance record: which due date is right?",
                [(eid, "the invoice record"), (mails[f["number"]], "the invoice e-mail")],
                [("due_date", f["due_date"])],
                difficulty="medium",
                rationale="The record holds the due date; the e-mail gives it in words.",
            )

    def stale(self) -> None:
        amended = [
            (e, lns) for e, lns in self.sim("contract") if lns[-1]["payload"]["fields"].get("last_amendment")
        ]
        for eid, lns in _spread(amended, MINIMUM["stale"] + 2):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "stale",
                f"What is the latest change to the contract of {f['client_name']}?",
                [(eid, "the contract and its amendments")],
                [("last_amendment", f["last_amendment"])],
                difficulty="medium",
                rationale="Only the newest version of the contract is current.",
            )

    def impact(self) -> None:
        big = [
            (e, lns)
            for e, lns in self.sim("incident")
            if lns[-1]["payload"]["fields"].get("severity") in ("SEV1", "SEV2")
            and lns[-1]["payload"]["fields"].get("client_name")
        ]
        for eid, lns in _spread(big, MINIMUM["impact"] + 2):
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
        deps = []
        for eid, lns in self.sim("deploy"):
            f = lns[0]["payload"]["fields"]
            for mr in f.get("related") or []:
                if mr in self.by_entity:
                    deps.append((eid, mr, f["version"]))
                    break
        for dep, mr, version in _spread(deps, MINIMUM["multi_hop"] + 2):
            key = self.by_entity[mr][-1]["payload"]["title"].split(":")[0]
            if f"jira.{key}" not in self.by_entity:
                continue
            self.add(
                "multi_hop",
                f"In which release did the work on {key} reach production?",
                [
                    (f"jira.{key}", "the ticket"),
                    (mr, "the merge request that closes it"),
                    (dep, "the deploy"),
                ],
                [("version", version)],
                difficulty="hard",
                rationale="Ticket -> merge request (Closes) -> deploy that shipped it.",
            )

    def acl(self) -> None:
        payrolls = [e for e, _ in self.sim("payroll")]
        engineers = sorted(
            p.person.login
            for p in self.w.people.values()
            if p.person.role in ("backend", "frontend", "qa", "devops") and p.last_day is None
        )
        if "sim.PROC-PAYROLL" not in self.by_entity or not engineers:
            return
        for k, eid in enumerate(_spread(payrolls, MINIMUM["acl"] + 2)):
            title = self.last(eid)["fields"]["title"]
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
                        "why": "payroll is visible to Finance and Management only",
                        "facts": [{"key": "payroll", "any": [title]}],
                    }
                ],
                rationale="An engineer sees the payroll process but not the payroll amounts.",
            )

    def negative(self) -> None:
        if self.all_text is None:
            self.all_text = " ".join(_text(ln.get("payload")) for ln in self.lines)
        for q, kw in NEGATIVE:
            if kw.casefold() in self.all_text:
                continue
            self.add(
                "negative",
                q,
                [],
                [],
                not_found=True,
                difficulty="medium",
                rationale=f"Nothing in the history mentions it. Keywords: {kw}",
            )

    def implicit_ref(self) -> None:
        rows = []
        for x in self.implicit:
            m, i = x["mail"], x["issue"]
            if m.ext and i.ext and f"mail.{m.ext}" in self.by_entity and f"jira.{i.ext}" in self.by_entity:
                rows.append((f"mail.{m.ext}", f"jira.{i.ext}", x["quote"], m.subject))
        for mail, issue, quote, subject in _spread(rows, MINIMUM["implicit_ref"] + 2):
            self.add(
                "implicit_ref",
                f"Which ticket does the e-mail '{subject}' mean by \"{quote}\"?",
                [(mail, "the client update"), (issue, "the ticket it refers to")],
                [("quote", quote)],
                difficulty="hard",
                rationale="The update names the work by its summary, without the key.",
            )

    def people(self) -> None:
        issues = [
            (e, lns)
            for e, lns in self.by_entity.items()
            if e.startswith("jira.")
            and (lns[-1]["payload"]["fields"].get("assignee") or {}).get("displayName")
            and lns[-1]["payload"]["fields"]["issuetype"]["name"] in ("Story", "Bug")
        ]
        for eid, lns in _spread(sorted(issues), MINIMUM["people"] + 2):
            f = lns[-1]["payload"]["fields"]
            self.add(
                "people",
                f"Who worked on {eid.removeprefix('jira.')} ({f['summary']})?",
                [(eid, "the ticket")],
                [("assignee", f["assignee"]["displayName"])],
                rationale="The assignee of the ticket.",
            )


def build_cases(
    w: World, lines: list[dict[str, Any]], implicit: list[dict[str, Any]], slug: str
) -> list[dict]:
    return CaseBuilder(w, lines, implicit, slug).build()
