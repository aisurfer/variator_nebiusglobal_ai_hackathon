"""Deterministic (template) answers from tool results — the baseline that works without an LLM (07 §12: a useful
mode must survive without generation). Every statement names the version, both times and the link origin."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from kb.reasoning.router import ParsedQuestion
from kb.reasoning.timeparse import TimeInterpretation
from kb.reasoning.tools import ToolContext

STATUS_EN = {
    "CURRENT": "holds",
    "CURRENT_COMPATIBLE": "holds (the cause changed, but not in the fields that matter)",
    "STALE": "STALE: the cause changed in the fields that matter",
    "BROKEN": "BROKEN: the cause was deleted",
    "RETRACTED": "retracted",
}
ORIGIN_EN = {"declared": "declared in the source", "inferred": "inferred automatically, needs a review"}
KEY_FIELDS = {
    "Meeting": ("held_at", "attendees"),
    "Decision": ("statement", "owner", "decided_at"),
    "Experiment": ("hypothesis", "start_date", "duration_days", "status", "result"),
    "Report": ("findings",),
    "Release": ("tag", "released_at"),
    "MergeRequest": ("state", "merged_at"),
    "Policy": ("rules",),
    "Epic": ("summary", "status"),
    "Story": ("summary", "status"),
    "Task": ("summary", "status"),
    "Bug": ("summary", "status"),
}


_TZ: ContextVar[str] = ContextVar("kb_answer_tz", default="UTC")


def _t(v: Any) -> str:
    """Times are shown in the tenant's timezone with its offset."""
    if v is None:
        return "—"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if v.tzinfo is not None:
        v = v.astimezone(ZoneInfo(_TZ.get()))
        return v.strftime("%Y-%m-%d %H:%M (UTC%z)").replace("00)", ")").replace("UTC+0300", "UTC+3")
    return v.strftime("%Y-%m-%d %H:%M")


def localize(obj: Any, depth: int = 0) -> Any:
    """Copy of tool output with every datetime rendered in the tenant timezone (for the LLM evidence pack)."""
    if depth > 10:
        return obj
    if isinstance(obj, datetime):
        return _t(obj)
    if isinstance(obj, dict):
        return {k: localize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [localize(v, depth + 1) for v in obj]
    return obj


def _status(s: str | None) -> str | None:
    return STATUS_EN.get(s or "", s)


def _q(title: Any) -> str:
    return f"“{title}”"


def _origin(o: str | None) -> str:
    o = o or ""
    for k, v in ORIGIN_EN.items():
        if o.startswith(k):
            return f"{v} ({o})"
    return o


def _short(v: Any, n: int = 160) -> str:
    s = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _node_line(n: dict[str, Any]) -> str:
    vid = n.get("version_id") or n.get("vid")
    fields = n.get("key_fields") or n.get("fields") or {}
    extras = [f"{k}: {_short(fields[k])}" for k in KEY_FIELDS.get(n.get("kind", ""), ()) if fields.get(k)]
    when = (f"effective {_t(n.get('effective_from'))}, recorded {_t(n.get('recorded_at'))}")
    tail = f"; {'; '.join(extras)}" if extras else ""
    deleted = " [deleted]" if n.get("deleted") else ""
    return f"{_q(n.get('title'))} ({n.get('kind')}, [{vid}]; {when}{tail}){deleted}"


def _ev_node(n: dict[str, Any], role: str) -> dict[str, Any]:
    return {"version_id": n.get("version_id"), "entity_id": n.get("entity_id"), "kind": n.get("kind"),
            "title": n.get("title"), "effective_from": n.get("effective_from"), "recorded_at": n.get("recorded_at"),
            "fields": {k: v for k, v in (n.get("key_fields") or n.get("fields") or {}).items()
                       if k in KEY_FIELDS.get(n.get("kind", ""), ()) or k in ("summary", "statement")},
            "role": role}


def _headline(res: dict[str, Any]) -> str | None:
    """One sentence for a manager: the chain through a meeting/decision, with when, who and on what basis."""
    nodes: dict[str, dict] = res.get("nodes") or {}

    def meeting_pos(ch: dict) -> int:  # the closest meeting to the effect is the direct reason
        vs = ch.get("versions") or []
        return next((i for i, v in enumerate(vs) if nodes.get(v, {}).get("kind") == "Meeting"), 10**6)

    for ch in sorted(res.get("chains") or [], key=meeting_pos):
        versions = ch.get("versions") or []
        meeting = next((nodes[v] for v in versions if nodes.get(v, {}).get("kind") == "Meeting"), None)
        decision = next((nodes[v] for v in versions if nodes.get(v, {}).get("kind") == "Decision"), None)
        if not meeting:
            continue
        mf = meeting.get("key_fields") or meeting.get("fields") or {}
        when = _t(mf.get("held_at") or meeting.get("effective_from"))
        who = ", ".join(mf.get("attendees") or [])
        target = nodes.get(versions[0], {}) if versions else {}
        text = "In short: " + f"{_q(target.get('title'))} [{versions[0]}] — "
        if decision:
            df = decision.get("key_fields") or {}
            text += ("by the decision "
                     + f"{_q(df.get('statement') or decision.get('title'))} [{decision['version_id']}]"
                     + (f" (owner: {df['owner']})" if df.get("owner") else "")
                     + ", made ")
        else:
            text += "following the discussion "
        text += (f"on {when} at the meeting “{meeting.get('title')}” [{meeting['version_id']}]")
        if who:
            text += f" (attendees: {who})"
        after = versions[versions.index(meeting["version_id"]) + 1:] if meeting["version_id"] in versions else []
        if after:
            basis, root = nodes.get(after[0], {}), nodes.get(after[-1], {})
            text += (f"; the meeting rested on “{basis.get('title')}” [{basis.get('version_id')}]"
                       f" of {_t(basis.get('effective_from'))}")
            if len(after) > 1:
                text += (f"; the root cause of the chain is “{root.get('title')}” [{root.get('version_id')}]"
                           f" of {_t(root.get('effective_from'))}")
        return text + "."
    return None


def _why(res: dict[str, Any], subject: dict | None) -> tuple[list[str], list[dict], list[str], bool]:
    lines: list[str] = []
    evidence: list[dict] = []
    cites: list[str] = []
    nodes: dict[str, dict] = res.get("nodes") or {}
    links: list[dict] = res.get("links") or []
    target = res.get("target") or {}
    tv = target.get("version_id")
    if not links:
        name = _q((subject or {}).get("label") or target.get("title"))
        return [f"No declared causes found for {name}."], [], [], False
    head = _headline(res)
    if head:
        lines.append(head)
    chains = res.get("chains") or []
    if chains:
        lines.append(("Chains from the effect to the root cause (declared links")
                     + (("; “?” marks an inferred link not reviewed by a person):")
                        if any(c.get("inferred") for c in chains) else "):"))
        shown = chains[:4]
        for ch in shown:
            text = ch.get("text")
            if not text:
                text = f"[{tv}] " + " ".join(f"—{st.get('predicate_label') or st.get('predicate')}→ [{st.get('cause_version')}]"
                                             for st in ch.get("steps") or [])
            lines.append(f"• {text}")
            cites += [v for v in ch.get("versions") or []]
        if len(chains) > len(shown):
            lines.append((f"  … and {len(chains) - len(shown)} more chains (the full list is in results.why.chains)."))
    roots = [nodes[r] for r in res.get("roots") or [] if r in nodes]
    if roots:
        lines.append("Root causes:")
        for rn in sorted(roots, key=lambda n: str(n.get("effective_from") or "")):
            lines.append(f"• {_node_line(rn)}")
            cites.append(rn.get("version_id"))
    flagged = [l for l in links if l.get("status") in ("STALE", "BROKEN")]
    if flagged:
        lines.append(("Grounds that changed since the decision (the historical fact stays):"))
        for l in flagged:
            now_is = l.get("cause_current_version")
            changed = ", ".join(l.get("changed_fields") or [])
            lines.append(f"• [{l.get('effect_version')}] —{l.get('predicate_label') or l.get('predicate')}→ "
                         f"[{l.get('cause_version')}]: {_status(l.get('status'))}"
                         + (f", now [{now_is}]" if now_is and now_is != l.get("cause_version") else "")
                         + (f", changed fields: {changed}" if changed else "")
                         + f"; the link is {_origin(l.get('origin'))}")
    inferred = [l for l in links if l.get("inferred") or str(l.get("origin", "")).startswith("inferred")]
    if inferred:
        lines.append((f"Hypotheses (inferred, not reviewed by a person): {len(inferred)}."))
    for n in nodes.values():
        evidence.append(_ev_node(n, "target" if n.get("version_id") == tv else "cause"))
    for l in links:
        ev = l.get("evidence") if isinstance(l.get("evidence"), dict) else {}
        evidence.append({"link_id": l.get("link_id"), "effect_version": l.get("effect_version"),
                         "cause_version": l.get("cause_version"), "predicate": l.get("predicate_label") or l.get("predicate"),
                         "causal_kind": l.get("causal_kind"), "origin": l.get("origin"), "status": l.get("status"),
                         "asserted_at": l.get("asserted_at"), "quote": ev.get("quote")})
    if tv:
        cites.insert(0, tv)
    return lines, evidence, [c for c in dict.fromkeys(cites) if c], True


def _affected(res: Any) -> list[dict]:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("affected", "impacted", "items", "entities", "dependents"):
            if isinstance(res.get(k), list):
                return res[k]
    return []


def _day(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    return v.astimezone(ZoneInfo(_TZ.get())).strftime("%Y-%m-%d") if v.tzinfo else v.strftime("%Y-%m-%d")


def _last_day(end: Any) -> str:
    """The last day of an exclusive end bound."""
    if isinstance(end, datetime):
        from datetime import timedelta

        return _day(end - timedelta(seconds=1))
    return _day(end)


def _list(plan: Any, res: dict[str, Any], count_only: bool) -> tuple[list[str], list[dict], list[str], list[str]]:
    """A list (or count) of entities chosen by the plan: complete up to the limit, with "shown N of M"."""
    lines: list[str] = []
    evidence: list[dict] = []
    cites: list[str] = []
    unknowns: list[str] = []
    items = res.get("items") or []
    total, shown = int(res.get("total") or 0), len(items)
    w = res.get("window") or {}
    period = (f"{_day(w.get('start'))} — {_last_day(w.get('end'))}" if (w.get("start") or w.get("end")) else None)
    what = ", ".join(plan.kinds) if plan and plan.kinds else "objects"
    if not total:
        cov = res.get("coverage")
        if period:
            msg = f"Nothing found for {period} ({what})."
            if cov:
                cs, ce = _day(cov.get("start")), _day(cov.get("end"))
                if w.get("start") and isinstance(cov.get("end"), datetime) and isinstance(w.get("start"), datetime) \
                        and w["start"] > cov["end"]:
                    msg += (f" There is no data for this period: the knowledge base ends on {ce} (data from {cs} "
                              f"to {ce}).")
                elif w.get("end") and isinstance(cov.get("start"), datetime) and isinstance(w.get("end"), datetime) \
                        and w["end"] <= cov["start"]:
                    msg += (f" There is no data for this period: the knowledge base starts on {cs} (data from {cs} "
                              f"to {ce}).")
                else:
                    msg += f" Data of this kind exists from {cs} to {ce}."
            if res.get("without_window"):
                msg += (f" Without the time limit {res['without_window']} match.")
            lines.append(msg)
        else:
            lines.append(f"Nothing found ({what}).")
        unknowns.append(("Nothing was found under these conditions in the sources available to you (this "
                           "does not prove the event did not happen)."))
        return lines, evidence, cites, unknowns
    head = f"Found: {total}" + (f" ({what}" + (f", {period}" if period else "") + ")")
    if count_only:
        lines.append(head + ".")
    else:
        lines.append(head + (f"; showing {shown} of {total}:" if shown < total
                             else ":"))
    for it in items if not count_only else items[:10]:
        people = it.get("people") or []
        lines.append(f"• {_day(it.get('event_at') or it.get('effective_from'))} — {_q(it.get('title'))} ({it.get('kind')}) "
                     f"[{it.get('version_id')}]" + (f"; people: {_short(people, 120)}"
                                                    if people else ""))
        cites.append(it.get("version_id"))
        evidence.append({"version_id": it.get("version_id"), "entity_id": it.get("entity_id"), "title": it.get("title"),
                         "kind": it.get("kind"), "effective_from": it.get("event_at") or it.get("effective_from"),
                         "people": people, "role": "item"})
    if count_only and total > 10:
        lines.append(f"  … the first 10 of {total}.")
    return lines, evidence, cites, unknowns


def compose(parsed: ParsedQuestion, subject: dict | None, alternatives: list[dict], results: dict[str, Any],
            ti: TimeInterpretation, ctx: ToolContext, plan: Any = None) -> dict[str, Any]:
    _TZ.set(ctx.tz)
    lines: list[str] = []
    evidence: list[dict] = []
    cites: list[str] = []
    unknowns: list[str] = []
    grounded_chain = False
    for a in results.get("asked") or []:
        # the asked field first: the direct answer, with the record and version it comes from
        when = f" (valid {_day(a.get('effective_from'))})" if a.get("effective_from") else ""
        lines.append(f"Answer: {a['field'].replace('_', ' ')} of {_q(a.get('title'))} is {_q(_short(a['value'], 160))}"
                     f"{when} [{a.get('version_id')}].")
        cites.append(a.get("version_id"))
        evidence.append({"version_id": a.get("version_id"), "entity_id": a.get("entity_id"), "title": a.get("title"),
                         "fields": {a["field"]: a["value"]}, "role": "asked"})
    if not subject and parsed.missing:
        lines.append("Nothing in the sources names " + ", ".join(_q(m) for m in parsed.missing)
                     + ": the knowledge base has no record of it. The nearest records below do not match.")
    if subject:
        lines.append((f"Subject: “{subject.get('label')}” ({subject.get('kind')}, {subject.get('id')}; "
                        f"matched: {subject.get('matched')})."))
    if alternatives:
        lines.append("Other objects may be meant: "
                     + "; ".join(f"{_q(a.get('label'))} ({a.get('id')})" for a in alternatives[:4])
                     + ". Say so if you meant another one.")
    if ti.interpretation:
        lines.append(f"Time: {ti.interpretation}.")
    if ctx.known_at:
        lines.append((f"Knowledge cut: as known at {_t(ctx.known_at)}."))
    if "list" in results:
        from kb.reasoning.plan import describe

        understood = describe(plan) if plan else ""
        if understood:
            lines.append(f"Understood: {understood}.")
        for n in (plan.nodes if plan else []):
            if (n.text or n.key) and not n.grounded:
                unknowns.append((f"Could not match “{n.text or n.key}” to an object of the knowledge base."))
            if n.alternatives:
                lines.append((f"“{n.text}” read as {_q(n.grounded[0].get('label'))}; others are possible: ")
                             + "; ".join(_q(a.get("label")) for a in n.alternatives[:3]) + ".")
        ll, ev, c, un = _list(plan, results["list"] or {}, parsed.intent == "count")
        lines += ll
        evidence += ev
        cites += c
        unknowns += un

    if "why" in results and parsed.intent in ("why", "timeline"):
        wl, ev, c, grounded_chain = _why(results["why"] or {}, subject)
        lines += wl
        evidence += ev
        cites += c
        lines.append(("Interpretation: “why” means the grounds relied upon when the decision was made (transaction "
                        "axis); the status shows whether they changed since."))
        if not grounded_chain:
            rel = results.get("related") or []
            if isinstance(rel, dict):
                rel = rel.get("items") or rel.get("results") or []
            if rel:
                lines.append(("Probably related (not declared, a graph hypothesis):"))
                for r in rel[:5]:
                    lines.append(f"• {_q(r.get('title'))} ({r.get('entity_id')}, score {round(float(r.get('score', 0)), 3)})")
            for h in (results.get("text_evidence") or [])[:5]:
                lines.append("• text: " + f"{_q(_short(h.get('snippet') or h.get('text'), 200))} [{h.get('version_id')}]")
                evidence.append({"version_id": h.get("version_id"), "title": h.get("title"), "quote": h.get("snippet") or h.get("text"),
                                 "recorded_at": h.get("recorded_at"), "effective_from": h.get("effective_from"), "role": "text"})
            unknowns.append(("No causal links of the object are declared in the sources."))
    if "impact" in results:
        res = results.get("impact")
        items = _affected(res)
        verb = ("a cancellation of" if parsed.hypothetical == "cancel" else
                "a change of " + ", ".join(parsed.fields or ["any fields"]))
        lines.append((f"What {verb} “{(subject or {}).get('label')}” affects: {len(items)} objects."))
        if isinstance(res, dict) and res.get("explanation_lines"):
            lines.append(res["explanation_lines"][0])
        for it in sorted(items, key=lambda x: (x.get("depth", 9), str(x.get("title"))))[:25]:
            via = it.get("via") or it.get("predicates") or it.get("path")
            lines.append(f"• depth {it.get('depth')}: "
                         + f"{_q(it.get('title'))} ({it.get('kind')}, {it.get('entity_id')})"
                         + (f" via {_short(via, 80)}" if via else ""))
            evidence.append({"entity_id": it.get("entity_id"), "version_id": it.get("version_id"), "title": it.get("title"),
                             "kind": it.get("kind"), "depth": it.get("depth"), "via": via, "role": "affected"})
        if isinstance(res, dict) and res.get("links"):
            lines.append((f"Links that become stale/broken: {len(res['links'])}."))
        lines.append(("This is a what-if: no data changed; only declared links and their field sensitivity "
                        "are taken into account."))
    if "stale" in results:
        items = _affected(results["stale"]) or (results["stale"] if isinstance(results["stale"], list) else [])
        lines.append((f"Rely on changed or deleted grounds: {len(items)}."))
        for it in items[:25]:
            eff = it.get("effect_title") or it.get("title") or it.get("effect_entity_id") or it.get("entity_id")
            cause = it.get("cause_title") or it.get("cause_entity_id")
            if it.get("explanation"):
                lines.append(f"• {it['explanation']}")
            else:
                lines.append(f"• {_q(eff)} [{it.get('effect_version')}] " + "relies on "
                             + f"{_q(cause)} [{it.get('cause_version')}]: {_status(it.get('status'))}"
                             + (f", now [{it.get('cause_current_version')}]"
                                if it.get("cause_current_version") else "")
                             + ("; changed fields: " + ", ".join(it.get("changed_fields") or [])
                                if it.get("changed_fields") else ""))
            evidence.append({**{k: it.get(k) for k in ("effect_version", "cause_version", "cause_current_version",
                                                       "status", "predicate", "origin")}, "role": "stale"})
            cites += [c for c in (it.get("effect_version"), it.get("cause_version")) if c]
    if "history" in results:
        hist = results["history"] or []
        lines.append(f"Versions in the period: {len(hist)}.")
        for h in hist[-12:]:
            fields = ", ".join(h.get("changed_fields") or []) or "—"
            lines.append((f"• [{h['version_id']}] {h['op']} — recorded {_t(h['recorded_at'])}, effective "
                            f"{_t(h['effective_from'])}; fields: {fields}"))
            cites.append(h["version_id"])
            evidence.append({"version_id": h["version_id"], "title": h.get("title"), "op": h["op"],
                             "recorded_at": h["recorded_at"], "effective_from": h["effective_from"],
                             "changed_fields": h.get("changed_fields"), "role": "version"})
        d = results.get("diff")
        if d:
            lines.append("Difference " + f"[{d['from']['version_id']}] → [{d['to']['version_id']}]:")
            for ch in d.get("changes", [])[:15]:
                if "before" in ch:
                    lines.append(f"  {ch['field']}: {_q(_short(ch['before'], 120))} → {_q(_short(ch['after'], 120))}")
                else:
                    lines.append(f"  {ch['field']}: " + "text changed")
            evidence.append({"diff": d.get("changes", [])[:15], "version_id": d["to"]["version_id"], "role": "diff"})
    if "timeline" in results:
        events = results["timeline"] or []
        lines.append(f"Timeline ({len(events)} events):")
        for e in events[:30]:
            lines.append(f"• {_t(e['effective_from'])} — {_q(e['title'])} ({e['kind']}) {e['op']} [{e['version_id']}]")
            cites.append(e["version_id"])
            evidence.append({"version_id": e["version_id"], "title": e["title"], "kind": e["kind"], "op": e["op"],
                             "effective_from": e["effective_from"], "recorded_at": e["recorded_at"], "role": "event"})
    if "entity" in results and results["entity"]:
        card = results["entity"]
        head = card.get("head") or {}
        lines.append((f"“{card['title']}” ({card['kind']}), current version [{head.get('version_id')}], recorded "
                        f"{_t(head.get('recorded_at'))}; versions in total: {card.get('versions')}."))
        cites.append(head.get("version_id"))
        evidence.append({"version_id": head.get("version_id"), "title": card["title"], "kind": card["kind"],
                         "fields": head.get("fields"), "role": "entity"})
    if "search" in results:
        hits = results["search"] or []
        if hits:
            lines.append("Fragments found:")
        for h in hits[:8]:
            lines.append(f"• {_q(h.get('title'))} [{h.get('version_id')}]: {_short(h.get('snippet') or h.get('text'), 360)}")
            cites.append(h.get("version_id"))
            evidence.append({"version_id": h.get("version_id"), "title": h.get("title"), "kind": h.get("kind"),
                             "quote": h.get("snippet") or h.get("text"), "recorded_at": h.get("recorded_at"),
                             "effective_from": h.get("effective_from"), "role": "fragment"})
        if not hits and not subject:
            unknowns.append(("Nothing was found in the sources available to you (this does not prove the event did "
                               "not happen)."))
    if not subject and parsed.intent in ("why", "impact", "changes", "lookup"):
        unknowns.append(("Could not tell which object the question is about — name it or give its key (e.g. EXP-7)."))
    return {"text": "\n".join(lines), "evidence": localize(evidence), "citations": list(dict.fromkeys(c for c in cites if c)),
            "unknowns": unknowns, "grounded_chain": grounded_chain}
