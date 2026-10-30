"""Deterministic explanations in English (research §5.5): every sentence
names the version, both times (effective and recorded), the origin of the link and its status. No LLM here: the
ask layer may rephrase, never invent."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from kb.causal._base import Labels, fmt_dt

_PROVIDERS = {"jira": "Jira", "confluence": "Confluence", "gitlab": "GitLab", "directory": "the employee directory",
              "email": "mail"}


def _lower(label: str) -> str:
    return label[:1].lower() + label[1:] if label and not label[:2].isupper() else label


def _q(title: Any) -> str:
    return f"“{title}”"


def _list(value: Any, limit: int = 8) -> str:
    if isinstance(value, list):
        items = [str(x.get("name") or x.get("title") or x) if isinstance(x, dict) else str(x) for x in value]
        return ", ".join(items[:limit]) + (" …" if len(items) > limit else "")
    return str(value)


def _provider(p: str) -> str:
    return _PROVIDERS.get(p, p)


def origin_phrase(origin: str, asserted_by: str | None = None) -> str:
    if origin == "declared:editor":
        who = f" {asserted_by}" if asserted_by else ""
        return f"declared by the editor{who} ({origin})"
    if origin == "declared:review":
        return f"confirmed in candidate review ({origin})"
    if origin.startswith("declared:") and origin.endswith("_import"):
        provider = origin[len("declared:"):-len("_import")]
        return (f"declared in the source, {_provider(provider)} ({origin})")
    if origin.startswith("declared:"):
        return f"declared ({origin})"
    if origin.startswith("inferred:"):
        return f"inferred automatically, not reviewed ({origin})"
    return origin


def node_ref(n: dict, *, with_times: bool = True, tz: ZoneInfo | None = None) -> str:
    base = f"{_lower(n['kind_label'])} {_q(n['title'])}"
    if not with_times or tz is None:
        return f"{base} ({n['version_id']})"
    return (f"{base} in version v{n['seq']} ({n['version_id']}; effective {fmt_dt(n['effective_from'], tz)}, "
              f"recorded {fmt_dt(n['recorded_at'], tz)})")


def key_bits(n: dict, tz: ZoneInfo) -> str:
    kf = n.get("key_fields") or {}
    bits = []
    if n["kind"] == "Meeting":
        if kf.get("held_at"):
            bits.append(f"held {fmt_dt(kf['held_at'], tz)}")
        if kf.get("attendees"):
            bits.append(f"attendees: {_list(kf['attendees'])}")
    elif n["kind"] == "Decision":
        if kf.get("statement"):
            bits.append(f"statement: {_q(kf['statement'])}")
        if kf.get("owner"):
            bits.append(f"owner: {_list(kf['owner'])}")
        if kf.get("decided_at"):
            bits.append(f"decided {fmt_dt(kf['decided_at'], tz)}")
    elif n["kind"] == "Experiment":
        if kf.get("hypothesis"):
            bits.append(f"hypothesis: {_q(kf['hypothesis'])}")
        if kf.get("start_date"):
            bits.append(f"started {fmt_dt(kf['start_date'], tz)}")
    if n.get("actor"):
        bits.append(f"version author: {n['actor']}")
    return "; ".join(bits)


def status_phrase(link: dict, nodes: dict[str, dict], known_at: datetime | None, tz: ZoneInfo) -> str:
    when = "now" if known_at is None else f"as of {fmt_dt(known_at, tz)}"
    status = link["status"]
    cause = nodes.get(link["cause_version"], {})
    title = _q(cause.get("title", link["cause_entity_id"]))
    cur = link.get("cause_current_version") or link["cause_version"]
    cur_seq = cur.rpartition("@")[2]
    first = link.get("changed_by_version") or {}
    if status == "CURRENT":
        text = (f"{when} the dependency holds (CURRENT): {title} has not changed since v{link['cause_seq']}")
    elif status == "CURRENT_COMPATIBLE":
        sens = ", ".join(link["sensitivity"] or []) or "—"
        text = (f"{when} the dependency is compatible (CURRENT_COMPATIBLE): {title} is at version v{cur_seq}, "
                  f"but the changes did not touch the fields the link depends on ({sens})")
    elif status == "STALE":
        fields = ", ".join(link.get("changed_fields") or []) or "any fields"
        text = (f"{when} the dependency is stale (STALE): {title} changed in version v{first.get('seq', cur_seq)} "
                  f"(effective {fmt_dt(first.get('effective_from'), tz)}, recorded "
                  f"{fmt_dt(first.get('recorded_at'), tz)}; changed: {fields}), current version v{cur_seq}; "
                  f"no review of the link is recorded")
    elif status == "BROKEN":
        text = (f"{when} the link is broken (BROKEN): {title} was deleted (recorded "
                  f"{fmt_dt(first.get('recorded_at'), tz)}); the historical fact stays")
    elif status == "RETRACTED":
        reason = f": {link['retract_reason']}" if link.get("retract_reason") else ""
        text = f"the link was retracted (RETRACTED{reason})"
    else:
        text = f"status {status}"
    if link.get("status_now") and link["status_now"] != status:
        text += f"; by current knowledge — {link['status_now']}"
    return text


def link_sentence(link: dict, nodes: dict[str, dict], labels: Labels, known_at: datetime | None, tz: ZoneInfo) -> str:
    eff = nodes.get(link["effect_version"])
    cause = nodes.get(link["cause_version"])
    if eff is None or cause is None:
        return ""
    prefix = "candidate: " if link.get("inferred") else ""
    return (f"{prefix}{eff['kind_label']} {_q(eff['title'])} ({eff['version_id']}) — “{link['predicate_label']}” → "
              f"{node_ref(cause, tz=tz)}; the link is {origin_phrase(link['origin'], link.get('asserted_by'))}, "
              f"{link['link_id']}, recorded {fmt_dt(link['asserted_at'], tz)}; {status_phrase(link, nodes, known_at, tz)}.")


def chain_text(chain: dict, nodes: dict[str, dict], links: dict[str, dict], labels: Labels) -> str:
    parts = [f"{_q(nodes[chain['versions'][0]]['title'])} ({chain['versions'][0]})"]
    for lid, vid in zip(chain["links"], chain["versions"][1:]):
        link = links[lid]
        mark = "?" if link["inferred"] else ""
        status = "" if link["status"] in ("CURRENT", "CURRENT_COMPATIBLE") else f" [{link['status']}]"
        parts.append(f"—{link['predicate_label']}{mark}{status}→ {_q(nodes[vid]['title'])} ({vid})")
    return " ".join(parts)


def why_interpretation(known_at: datetime | None, tz: ZoneInfo) -> str:
    base = ("“Why” means the grounds at the time of the decision (transaction axis): each link is bound to the "
              "version of the cause that was current when the link was recorded; the status shows whether that ground "
              "still holds")
    if known_at is None:
        return base + " by current knowledge."
    return base + (f" as known at {fmt_dt(known_at, tz)} (known_at).")


def why_lines(target: dict, direct: list[dict], nodes: dict[str, dict], links: list[dict], roots: list[str],
              chains: list[dict], known_at: datetime | None, tz: ZoneInfo, labels: Labels) -> list[str]:
    head = (f"{target['kind_label']} {_q(target['title'])} ({target['version_id']}; effective "
              f"{fmt_dt(target['effective_from'], tz)}, recorded {fmt_dt(target['recorded_at'], tz)})")
    if target.get("deleted"):
        head += ", now deleted"
    when = "" if known_at is None else (f" as known at {fmt_dt(known_at, tz)}")
    lines: list[str] = []
    if not direct:
        lines.append((f"No causal links are declared for {head}{when}."))
        lines.append(why_interpretation(known_at, tz))
        return lines
    lines.append(f"{head} exists because{when}:")
    for link in direct:
        cause = nodes[link["cause_version"]]
        prefix = "candidate (not reviewed): " if link["inferred"] else ""
        since = f" (stated in {link['effect_version']})" \
            if link["effect_version"] != target["version_id"] else ""
        lines.append((f"— {prefix}“{link['predicate_label']}”{since} → {node_ref(cause, tz=tz)}; the link is "
            f"{origin_phrase(link['origin'], link.get('asserted_by'))}, {link['link_id']}, recorded "
            f"{fmt_dt(link['asserted_at'], tz)}; {status_phrase(link, nodes, known_at, tz)}."))
    # meetings and decisions on the way are what a manager asks about
    seen: set[str] = set()
    for kind, intro in (("Decision", "Decision"), ("Meeting", "Meeting")):
        for vid, n in sorted(nodes.items(), key=lambda kv: (kv[1].get("depth") or 0, kv[0])):
            if n["kind"] != kind or vid == target["version_id"] or n.get("role") != "cause" or vid in seen:
                continue
            seen.add(vid)
            bits = key_bits(n, tz)
            lines.append((f"{intro} {_q(n['title'])} ({vid}; effective {fmt_dt(n['effective_from'], tz)}, recorded "
                            f"{fmt_dt(n['recorded_at'], tz)}){': ' + bits if bits else ''}."))
    if roots:
        items = []
        for vid in roots:
            n = nodes[vid]
            bits = key_bits(n, tz)
            gone = ", deleted" if n["deleted"] else ""
            items.append((f"{_lower(n['kind_label'])} {_q(n['title'])} ({vid}, recorded {fmt_dt(n['recorded_at'], tz)}")
                         + f"{gone}{'; ' + bits if bits else ''})")
        lines.append("Root causes: " + "; ".join(items) + ".")
    best: dict[str, dict] = {}  # the best chain to each first cause, in chain order
    for c in chains:
        if c["complete"] and c["root"] not in best:
            best[c["root"]] = c
    for i, c in enumerate(list(best.values())[:5], 1):
        lines.append(f"Chain {i}: {c.get('text', '')}.")
    deeper = [x for x in links if x.get("depth", 1) > 1 and x["status"] in ("STALE", "BROKEN")]
    for link in deeper[:6]:
        eff = nodes.get(link["effect_version"])
        if eff is None:
            continue
        lines.append((f"Deeper in the chain: {eff['kind_label']} {_q(eff['title'])} ({eff['version_id']}) — "
                        f"“{link['predicate_label']}” → {_q(nodes[link['cause_version']]['title'])} "
                        f"({link['cause_version']}): {status_phrase(link, nodes, known_at, tz)}."))
    lines.append(why_interpretation(known_at, tz))
    return lines


def affected_lines(affected: list[dict], labels: Labels, limit: int = 12) -> list[str]:
    out = []
    for a in affected[:limit]:
        via = " → ".join(_q(labels.predicate(s["predicate"])) for s in (a["paths"][0] if a["paths"] else []))
        tag = " (only through unreviewed links)" if a.get("inferred_only") else ""
        out.append((f"— {_lower(labels.kind(a['kind']))} {_q(a['title'])} ({a['current_version']}), depth "
                      f"{a['depth']}, path: {via or '—'}, weight {a['score']}{tag}"))
    if len(affected) > limit:
        out.append(f"— and {len(affected) - limit} more")
    return out
