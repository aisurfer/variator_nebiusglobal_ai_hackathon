"""/ask (flow F4): question + now -> query plan over the semantic graph -> executor -> evidence -> answer.

The plan (kb.reasoning.plan) is made by one LLM call in `auto` (validated, cached) and by the rules in
`deterministic` or when the LLM is unavailable; the same executor (kb.reasoning.execute) runs it.
The deterministic pipeline always runs and always yields an answer built from tool results (template mode).
With an LLM available, `auto` rewrites that evidence into a fluent answer (citations validated against the
evidence the tools actually returned). `agent` is accepted for API compatibility and behaves like `auto`: Plancy's
bounded tool-calling investigation loop is not part of this copy. Answers are English.
An answer always names versions, both times, the origin of links and the interpretation of the question.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import orjson
import psycopg

from kb.acl import PrincipalCtx
from kb.db import Jsonb
from kb.llm.client import Budget, BudgetExceeded, get_llm
from kb.reasoning import answer as compose
from kb.reasoning.execute import execute
from kb.reasoning.plan import PlanError, QueryPlan, Vocabulary, describe, load_vocabulary, plan_llm, plan_rules
from kb.reasoning.router import parse_rules
from kb.reasoning.timeparse import TimeInterpretation, resolve_time
from kb.reasoning.tools import ToolContext
from kb.reasoning.tools import call as _call
from kb.worker.framework import watermarks

log = logging.getLogger(__name__)


def _tenant_tz(conn: psycopg.Connection, tenant_id: str) -> str:
    row = conn.execute("SELECT settings FROM tenant WHERE tenant_id = %s", (tenant_id,)).fetchone()
    return ((row or {}).get("settings") or {}).get("timezone", "UTC")


def _ground_event(ctx: ToolContext, text: str) -> dict | None:
    """Event anchors of time expressions ("before release v2.4.0") -> entity head version and its effective time."""
    cands = _call(ctx, "ground", {"text": text, "kind": "entity",
                                 "entity_kinds": ["Release", "Meeting", "Decision", "Experiment"], "limit": 1})
    if not cands:
        return None
    card = _call(ctx, "entity", {"entity_id": cands[0]["id"]})
    if not card or not card.get("head"):
        return None
    head = card["head"]
    when = (head.get("fields") or {}).get("held_at") or (head.get("fields") or {}).get("released_at") or head["effective_from"]
    return {"entity_id": card["entity_id"], "title": card["title"], "version_id": head["version_id"],
            "effective_from": when}


def _merge_time(plan: QueryPlan, ti: TimeInterpretation, tz: str, now: datetime, vocab: Vocabulary) -> TimeInterpretation:
    """One time interpretation for the plan. Where the rule parser recognised the expression its arithmetic is
    kept (documented semantics, no model arithmetic); the LLM plan covers what the rules do not know."""
    t = plan.time
    if plan.planner != "rules" and not (t.start or t.end or t.known_at) and ti.confidence < 1.0:
        # the LLM saw no time in the question and the rules only guessed one from a loose date search
        plan.notes.append((f"a loose date guess was ignored: {ti.interpretation}"))
        return TimeInterpretation()
    if plan.planner != "rules" and plan.operation in ("list", "count") and t.event == "occurred":
        # a state transition the rules recognised (closed, merged) is kept
        rules = plan_rules(plan.question, now=now, tz=tz, vocab=vocab, ti=ti)
        if rules and rules.time.event == "state" and set(rules.kinds) & set(plan.kinds):
            t.event, t.state_field, t.state_values = "state", rules.time.state_field, rules.time.state_values
            plan.notes.append("the state transition comes from the rules: "
                              + f"{t.state_field} → {', '.join(t.state_values)}")
    if plan.planner == "rules" or (ti.start or ti.end or ti.known_at):
        if plan.planner != "rules" and (t.start, t.end) != (ti.start, ti.end) and (t.start or t.end):
            plan.notes.append(("the time window comes from the rule "
                                 "date parser: ") + (ti.interpretation or ""))
        t.start, t.end, t.expression, t.interpretation = ti.start, ti.end, ti.expression, ti.interpretation
        t.known_at = ti.known_at or t.known_at
        t.source = "rules" if not ti.empty else t.source
        if t.known_at and not ti.known_at:
            ti.known_at = t.known_at
        return ti
    if not (t.start or t.end or t.known_at):
        return ti
    zone = ZoneInfo(tz)
    span = (f"[{t.start.astimezone(zone).isoformat(timespec='minutes') if t.start else '…'}, "
            f"{t.end.astimezone(zone).isoformat(timespec='minutes') if t.end else '…'})")
    interp = (t.interpretation or "time window of the plan")
    if t.start or t.end:
        interp = f"{interp} {span}"
    if t.known_at:
        interp += (f"; knowledge at {t.known_at.isoformat(timespec='minutes')} (transaction axis)")
    t.interpretation = interp
    return TimeInterpretation(expression=t.expression, start=t.start, end=t.end, known_at=t.known_at,
                              granularity="plan", interpretation=interp, confidence=0.9)


def ask(
    conn: psycopg.Connection,
    p: PrincipalCtx,
    question: str,
    *,
    now: datetime | None = None,
    known_at: datetime | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    out = _ask(conn, p, question, now=now, known_at=known_at, mode=mode)
    out["language"] = "en"
    return out


def _ask(conn: psycopg.Connection, p: PrincipalCtx, question: str, *, now: datetime | None,
         known_at: datetime | None, mode: str) -> dict[str, Any]:
    if now is None:  # relative expressions are read from the date of view, else from the end of the data
        from kb.asof import bounds

        now = known_at or bounds(conn, p.tenant_id).end or datetime.now(timezone.utc)
    tz = _tenant_tz(conn, p.tenant_id)
    ctx = ToolContext(conn, p, now=now, known_at=known_at, tz=tz)
    budget = Budget(max_calls=4, max_input_tokens=32_000, max_output_tokens=4_000)
    llm = get_llm(budget) if mode in ("auto", "agent") else None
    ti = resolve_time(question, now, tz=tz, ground_event=lambda t: _ground_event(ctx, t))
    vocab = load_vocabulary(conn, p.tenant_id)
    warnings: list[str] = []
    plan: QueryPlan | None = None
    t0 = time.monotonic()
    if llm:
        try:
            plan = plan_llm(question, now=now, tz=tz, vocab=vocab, llm=llm)
        except BudgetExceeded:
            log.info("LLM budget exhausted before planning; rules plan")
        except (PlanError, Exception) as exc:  # the LLM is optional: rules take over, never an error
            log.warning("LLM plan failed (%s); rules plan", exc)
            warnings.append(("the LLM plan is unavailable; the "
                               "question was parsed by rules"))
    if plan is None:
        plan = plan_rules(question, now=now, tz=tz, vocab=vocab, ti=ti)
    plan_ms = round((time.monotonic() - t0) * 1000)
    ti = _merge_time(plan, ti, tz, now, vocab)
    if ti.known_at and (known_at is None or ti.known_at < known_at):
        ctx.known_at = ti.known_at  # «what did we know on May 1»; never after the date of view
    parsed = plan.to_parsed(parse_rules(question, ti.expression))

    results, subject, alternatives = execute(ctx, plan, vocab, parsed, ti, warnings)
    template = compose.compose(parsed, subject, alternatives, results, ti, ctx, plan=plan)
    answer_text, answer_mode, unknowns = template["text"], "template", template.get("unknowns", [])
    citations = template.get("citations", [])

    if llm and mode in ("auto", "agent") and plan.operation not in ("list", "count"):
        # a list is answered in full by the executor; rewriting it would drop items
        try:
            from kb.reasoning.agent import synthesize

            res = synthesize(ctx, llm, question, template=template, results=results, time=ti)
            if res and res.get("text"):
                answer_text, answer_mode = res["text"], res["mode"]
                citations = res.get("citations", citations)
                unknowns = res.get("unknowns", unknowns)
        except BudgetExceeded:
            log.info("LLM budget exhausted; template answer kept")
        except Exception:
            log.warning("LLM answer failed; template answer kept", exc_info=True)

    run_id = f"inv-{uuid.uuid4().hex[:12]}"
    out = {
        "run_id": run_id,
        "question": question,
        "answer": answer_text,
        "answer_mode": answer_mode,
        "template_answer": template["text"],
        "intent": parsed.intent,
        "parsed": parsed.as_dict(),
        "plan": {**plan.summary(), "understood": describe(plan), "ms": plan_ms},
        "subject": subject,
        "alternatives": alternatives,
        "time": ti.as_dict(),
        "known_at": ctx.known_at.isoformat() if ctx.known_at else None,
        "citations": citations,
        "unknowns": unknowns,
        "warnings": warnings,
        "evidence": template.get("evidence", []),
        "results": results,
        "receipts": ctx.receipts,
        "llm_usage": llm.usage.as_dict() if llm else None,
        "watermarks": watermarks(conn, p.tenant_id),
    }
    try:
        conn.execute(
            """INSERT INTO investigation_run (tenant_id, run_id, principal, question, mode, status, receipts,
                 evidence_ids, budget, answer) VALUES (%s, %s, %s, %s, %s, 'done', %s, %s, %s, %s)""",
            (p.tenant_id, run_id, p.principal_id, question, answer_mode, Jsonb(_jsonable(ctx.receipts)),
             sorted(ctx.evidence_ids)[:1000], Jsonb(out["llm_usage"] or {}),
             Jsonb({"text": answer_text, "citations": citations})),
        )
    except Exception:
        log.warning("could not persist investigation run", exc_info=True)
    return out


def _jsonable(v: Any) -> Any:
    return orjson.loads(orjson.dumps(v, default=str))
