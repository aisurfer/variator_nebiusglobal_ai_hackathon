"""Headless Nemotron writers: requests, calls, checks and retries (resumable).

Protocol, all on disk under ``<work>``:

    requests/<id>.json    the prompt, the JSON schema and the artifact ids of one call
    responses/<id>.json   the CLI result (structured output, cost, time, errors)
    texts.json            per artifact: status (accepted | failed | dropped), attempts, problems, texts

A round batches the artifacts that still need texts (3-5 per request), runs the missing
calls in parallel, and checks every answer. An artifact that fails gets a retry request
with its problems listed; after ``max_attempts`` failed attempts it is dropped and logged.
Re-running skips every request that already has a response.

The call uses the project Token Factory client with JSON-object output. It has
no tools, MCP servers, session persistence, or ambient provider credentials.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from variator.llm import TokenFactoryClient, extract_json
from variator.llm.models import ROLE_MODEL
from variator.narrative.check import Names, check_artifact
from variator.narrative.facts import Packet

SYSTEM = (
    "You are a careful business writer. You write in plain British business English only. "
    "You never add facts that are not in the packet you are given. You reply only with the requested JSON."
)
STYLE = """STYLE GUIDE
- Plain business English, the way a busy manager writes notes or e-mails. Short sentences. No marketing words.
- No headings, no bullet points, no markdown, no greetings inside page notes. \
E-mails start with a short greeting
  (first name) and end with the sender's first name.
- Colleagues by first name (people[].first_name). People outside the company by full name the first time.
- FACTS ONLY. Every number, amount, percentage, date, time, month, id and name you write must appear in the
  packet, copied exactly as written there. Do not round, convert, add up or compare numbers yourself. Do not
  write weekdays. Do not write ticket or record ids unless the packet gives them.
- Each field lists `must_include`: write each of those strings exactly as given (same words, same digits).
- Do not invent causes, decisions, promises, dates, plans, people, clients or tickets. \
When the packet does not
  say something, leave it out. Notes are observations, not decisions.
- Keep each field within its word range. English only. No URLs, no e-mail addresses."""
BATCH = 4
TYPE_ORDER = ["postmortem", "weekly_sync", "leadership_review", "decision_rationale", "qbr", "escalation"]


def nemotron_request(
    prompt: str, schema: dict[str, Any], model: str = ROLE_MODEL["narrative"]
) -> dict[str, Any]:
    """Build one no-tools Token Factory request that asks for the supplied JSON schema."""
    return {"prompt": prompt, "schema": schema, "model": model}


def schema_for(packets: list[Packet]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            pk.id: {
                "type": "object",
                "properties": {s: {"type": "string"} for s in pk.slots},
                "required": list(pk.slots),
                "additionalProperties": False,
            }
            for pk in packets
        },
        "required": [pk.id for pk in packets],
        "additionalProperties": False,
    }


def prompt_for(company: str, packets: list[Packet], retry: dict[str, dict[str, Any]] | None = None) -> str:
    docs = [pk.for_writer() for pk in packets]
    parts = [
        f"Write the text fields of the {len(docs)} documents below for {company}, a UK software company.",
        STYLE,
        "OUTPUT\nReturn one JSON object: one key per document id; "
        "its value has one key per field, with the text.",
    ]
    if retry:
        fixes = []
        for aid, r in retry.items():
            fixes.append(
                {"id": aid, "problems_of_your_previous_text": r["problems"], "previous_text": r["texts"]}
            )
        parts.append(
            "YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix every problem listed; keep what was right.\n"
            + json.dumps(fixes, ensure_ascii=False, indent=1)
        )
    parts.append("DOCUMENTS\n" + json.dumps(docs, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def batches(ids: list[str], types: dict[str, str], size: int = BATCH) -> list[list[str]]:
    order = sorted(ids, key=lambda a: (TYPE_ORDER.index(types[a]) if types[a] in TYPE_ORDER else 99, a))
    return [order[i : i + size] for i in range(0, len(order), size)]


def request_id(round_no: int, ids: list[str]) -> str:
    return f"r{round_no}-" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:10]


def call(request: dict[str, Any], cwd: Path, timeout: int) -> dict[str, Any]:
    """Call Nemotron and preserve the on-disk writer response contract."""
    t0 = time.monotonic()
    try:
        client = TokenFactoryClient()
        result = client.chat(
            request["model"],
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": request["prompt"]}],
            reasoning=bool(request.get("reasoning")),  # on for the card extractor (Ultra), off for the swarm
            response_format={"type": "json_object"},
            max_tokens=4_000,
        )
        structured = json.loads(extract_json(result.content))
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.monotonic() - t0, 1),
        }
    return {
        "ok": isinstance(structured, dict),
        "seconds": round(time.monotonic() - t0, 1),
        "structured": structured,
        "cost_usd": round(client.ledger.total_cost(), 6),
        "model": result.model,
    }


class Writers:
    def __init__(
        self,
        plan: dict[str, Any],
        work: str | Path,
        *,
        parallel: int = 6,
        timeout: int = 300,
        max_attempts: int = 3,
        model: str = ROLE_MODEL["narrative"],
        runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        log: Callable[[str], None] = print,
    ):
        self.plan = plan
        self.work = Path(work)
        self.arts = {a["id"]: a for a in plan["artifacts"]}
        self.types = {a["id"]: a["type"] for a in plan["artifacts"]}
        self.packets = {a["id"]: Packet.from_json(a["packet"]) for a in plan["artifacts"]}
        self.names = Names.from_json(plan["names"])
        self.parallel, self.timeout, self.max_attempts, self.model = parallel, timeout, max_attempts, model
        self.runner = runner or (lambda request: call(request, self.work / "cwd", self.timeout))
        self.log = log
        for d in ("requests", "responses", "cwd"):
            (self.work / d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------------ state
    def state_path(self) -> Path:
        return self.work / "texts.json"

    def load_state(self) -> dict[str, Any]:
        p = self.state_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    def save_state(self, st: dict[str, Any]) -> None:
        self.state_path().write_text(
            json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )

    # ------------------------------------------------------------------------------ rounds
    def run(self, ids: list[str] | None = None) -> dict[str, Any]:
        """Write, check and retry until every artifact is accepted or dropped."""
        wanted = list(ids or self.arts)
        st = self.load_state()
        for rnd in range(self.max_attempts):
            todo = [
                a
                for a in wanted
                if st.get(a, {}).get("status") not in ("accepted", "dropped")
                and len(st.get(a, {}).get("attempts", [])) == rnd
            ]
            if not todo:
                continue
            reqs = []
            for group in batches(todo, self.types):
                rid = request_id(rnd, group)
                retry = {a: st[a]["attempts"][-1] for a in group if rnd > 0}
                pks = [self.packets[a] for a in group]
                req = {
                    "id": rid,
                    "round": rnd,
                    "artifacts": group,
                    "schema": schema_for(pks),
                    "prompt": prompt_for(self.plan.get("company", "the company"), pks, retry or None),
                }
                rp = self.work / "requests" / f"{rid}.json"
                if not rp.is_file():
                    rp.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
                reqs.append(req)
            missing = [r for r in reqs if not (self.work / "responses" / f"{r['id']}.json").is_file()]
            self.log(f"round {rnd}: {len(todo)} artifacts in {len(reqs)} requests, {len(missing)} to call")
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
                for a in r["artifacts"]:
                    texts = (res.get("structured") or {}).get(a) if res.get("ok") else None
                    probs = (
                        check_artifact(self.packets[a], texts, self.names)
                        if texts
                        else [f"no answer ({res.get('error') or 'call failed'})"]
                    )
                    s = st.setdefault(a, {"attempts": []})
                    s["attempts"].append({"request": r["id"], "problems": probs, "texts": texts or {}})
                    if not probs:
                        s["status"] = "accepted"
                        s["texts"] = texts
                    elif len(s["attempts"]) >= self.max_attempts:
                        s["status"] = "dropped"
                    else:
                        s["status"] = "failed"
            self.save_state(st)
        return self.summary(st, wanted)

    def _call(self, req: dict[str, Any]) -> dict[str, Any]:
        return self.runner(nemotron_request(req["prompt"], req["schema"], self.model))

    def summary(self, st: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
        acc = [a for a in wanted if st.get(a, {}).get("status") == "accepted"]
        drop = [a for a in wanted if st.get(a, {}).get("status") == "dropped"]
        first = [a for a in acc if len(st[a]["attempts"]) == 1]
        out = {
            "artifacts": len(wanted),
            "accepted": len(acc),
            "accepted_first_try": len(first),
            "retried": sum(1 for a in wanted if len(st.get(a, {}).get("attempts", [])) > 1),
            "dropped": drop,
            "requests": len(list((self.work / "responses").glob("*.json"))),
        }
        costs, secs = [], []
        for p in (self.work / "responses").glob("*.json"):
            r = json.loads(p.read_text(encoding="utf-8"))
            costs.append(r.get("cost_usd") or 0.0)
            secs.append(r.get("seconds") or 0.0)
        out["cost_usd"] = round(sum(costs), 4)
        out["call_seconds_total"] = round(sum(secs), 1)
        if drop:
            (self.work / "dropped.json").write_text(
                json.dumps({a: st[a]["attempts"] for a in drop}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        return out


def recheck(plan: dict[str, Any], work: str | Path) -> dict[str, Any]:
    """Check every stored answer again (no calls); return accepted texts by artifact."""
    w = Writers(
        plan, work, runner=lambda cmd: {"ok": False, "error": "no calls in check"}, log=lambda s: None
    )
    st = w.load_state()
    out: dict[str, dict[str, str]] = {}
    report: dict[str, Any] = {"accepted": 0, "failed": 0, "dropped": 0, "problems": {}}
    for a, s in st.items():
        if a not in w.packets:
            continue
        att = s.get("attempts") or []
        good = next(
            (x for x in att if x.get("texts") and not check_artifact(w.packets[a], x["texts"], w.names)), None
        )
        if good:
            out[a] = good["texts"]
            report["accepted"] += 1
        else:
            report["dropped" if s.get("status") == "dropped" else "failed"] += 1
            report["problems"][a] = att[-1]["problems"] if att else ["no attempt"]
    return {"texts": out, "report": report}
