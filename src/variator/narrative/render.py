"""Turn planned artifacts and their texts into timeline lines and gold links.

Same payload formats and id schemes as the base renderer (``orgsim.company.render``):

- Confluence pages: v2 page JSON with an ADF body; the page-properties table (``details``
  macro) carries Type, Date, Attendees (mentions), Based on (a link: ``informed_by``) and the
  ids; decisions are ``decisionItem`` nodes (localId ``dec-N``), action items ``taskItem``
  nodes (localId ``task-N``) that mention the assignee and link the existing ticket;
- mail: Graph-like messages in the base thread (``inReplyTo``, ``references``), acl = the
  employee participants;
- Jira: a new last version of an existing ticket that adds one remote link to a page;
- sim: a new version of a decision record (full state, every cause that still holds).

Lines leave ``sequence``, ``version``, ``previous_event_id`` and ``event_id`` empty: the
delivery writer fills them, so version chains stay consistent with the base.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from variator.narrative.base import Base, parse_time
from variator.narrative.facts import Packet, placeholder_text
from variator.narrative.planner import SIM_SEQ_BASE, SPACES
from variator.orgsim.events import CAUSE_PREDICATES, SCHEMA_VERSION

LAG = timedelta(minutes=1)


def _jira_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _graph_time(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(
    source: str,
    otype: str,
    ext: str,
    t: datetime,
    payload: dict[str, Any],
    *,
    container: str | None = None,
    acl: list[str] | None = None,
) -> dict[str, Any]:
    at = (t + LAG).isoformat()
    return {
        "event_id": None,
        "sequence": None,
        "at": at,
        "world_at": t.isoformat(),
        "observed_at": at,
        "source": source,
        "object_type": otype,
        "external_id": ext,
        "container": container,
        "op": "upsert",
        "modified_at": t.isoformat(),
        "acl": acl,
        "version": None,
        "previous_event_id": None,
        "schema_version": SCHEMA_VERSION,
        "payload": payload,
    }


# ================================================================================================ ADF
def tx(s: str, href: str | None = None) -> dict[str, Any]:
    n: dict[str, Any] = {"type": "text", "text": s}
    if href:
        n["marks"] = [{"type": "link", "attrs": {"href": href}}]
    return n


def para(*inline: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(inline)}


def mention(b: Base, pid: str) -> dict[str, Any]:
    p = b.people[pid]
    return {"type": "mention", "attrs": {"id": p.account, "text": f"@{p.name}", "accessLevel": ""}}


def paragraphs(text: str) -> list[dict[str, Any]]:
    parts = [" ".join(x.split()) for x in text.replace("\r\n", "\n").split("\n\n")]
    return [para(tx(x)) for x in parts if x]


def properties(b: Base, props: list[dict[str, Any]]) -> dict[str, Any]:
    def cell(kind: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": kind, "attrs": {}, "content": [para(*content)]}

    rows = []
    for pr in props:
        inline: list[dict[str, Any]] = []
        if pr.get("mentions"):
            for n, pid in enumerate(pr["mentions"]):
                if n:
                    inline.append(tx(", "))
                inline.append(mention(b, pid))
            if pr.get("extra"):
                inline.append(tx(f"; {pr['extra']}"))
        elif pr.get("links"):
            for n, (label, url) in enumerate(pr["links"]):
                if n:
                    inline.append(tx(", "))
                inline.append(tx(label, url))
        else:
            inline.append(tx(str(pr.get("text", ""))))
        rows.append(
            {
                "type": "tableRow",
                "content": [cell("tableHeader", [tx(pr["label"])]), cell("tableCell", inline)],
            }
        )
    table = {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"}, "content": rows}
    return {
        "type": "bodiedExtension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "details",
            "parameters": {"macroParams": {}},
        },
        "content": [table],
    }


def page_doc(b: Base, art: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    pg = art["page"]
    content: list[dict[str, Any]] = [properties(b, pg["properties"])]
    for blk in pg["blocks"]:
        if "h" in blk:
            content.append({"type": "heading", "attrs": {"level": 2}, "content": [tx(blk["h"])]})
        elif "slot" in blk:
            content += paragraphs(texts[blk["slot"]])
        elif "bullets" in blk:
            content.append(
                {
                    "type": "bulletList",
                    "content": [{"type": "listItem", "content": [para(tx(x))]} for x in blk["bullets"]],
                }
            )
        elif "table" in blk:
            head = {
                "type": "tableRow",
                "content": [
                    {"type": "tableHeader", "attrs": {}, "content": [para(tx(x))]}
                    for x in ("Metric", "Value")
                ],
            }
            rows = [
                {
                    "type": "tableRow",
                    "content": [{"type": "tableCell", "attrs": {}, "content": [para(tx(str(c)))]} for c in r],
                }
                for r in blk["table"]
            ]
            content.append(
                {
                    "type": "table",
                    "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                    "content": [head, *rows],
                }
            )
        elif "decisions" in blk:
            items = []
            for n, d in enumerate(blk["decisions"], start=1):
                inline = [tx(d["text"] + " Owner: "), mention(b, d["owner"])]
                items.append(
                    {
                        "type": "decisionItem",
                        "attrs": {"localId": f"dec-{n}", "state": "DECIDED"},
                        "content": inline,
                    }
                )
            content.append({"type": "decisionList", "attrs": {"localId": "decisions"}, "content": items})
        elif "tasks" in blk:
            items = []
            for n, t in enumerate(blk["tasks"], start=1):
                verb, _, rest = t["text"].partition(f"{t['key']}")
                inline = [
                    mention(b, t["assignee"]),
                    tx(" " + verb),
                    tx(t["key"], b.issue_url(t["key"])),
                    tx(rest),
                ]
                items.append(
                    {
                        "type": "taskItem",
                        "attrs": {"localId": f"task-{n}", "state": t["state"]},
                        "content": inline,
                    }
                )
            content.append({"type": "taskList", "attrs": {"localId": "tasks"}, "content": items})
    return {"type": "doc", "version": 1, "content": content}


def page_payload(b: Base, art: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    pg = art["page"]
    sp = SPACES[pg["space"]]
    t = parse_time(art["publish"])
    acc = b.people[pg["author"]].account
    doc = page_doc(b, art, texts)
    return {
        "id": pg["id"],
        "status": "current",
        "title": pg["title"],
        "spaceId": sp["id"],
        "space": {"id": sp["id"], "key": pg["space"], "name": sp["name"]},
        "parentId": f"{sp['id']}0",
        "parentType": "page",
        "authorId": acc,
        "ownerId": acc,
        "createdAt": _graph_time(t),
        "version": {
            "number": 1,
            "message": "",
            "minorEdit": False,
            "authorId": acc,
            "createdAt": _graph_time(t),
        },
        "labels": {
            "results": [
                {"id": str(int(hashlib.sha1(x.encode()).hexdigest()[:6], 16)), "name": x, "prefix": "global"}
                for x in pg["labels"]
            ]
        },
        "body": {
            "atlas_doc_format": {
                "value": json.dumps(doc, ensure_ascii=False),
                "representation": "atlas_doc_format",
            }
        },
        "_links": {
            "webui": f"/spaces/{pg['space']}/pages/{pg['id']}/{pg['title'].replace(' ', '+')}",
            "base": b.base_urls["confluence"],
        },
        "text_slot": f"confluence:{pg['id']}:narrative",
    }


def page_entity(art: dict[str, Any]) -> str:
    return f"confluence.page-{art['page']['id']}"


# ================================================================================================ mail
def _addr(b: Base, who: list[str]) -> tuple[str, str]:
    kind, ident = who
    if kind == "person":
        p = b.people[ident]
        return p.name, p.email
    c = b.contacts[ident]
    return c.name, c.email


def thread_lines(b: Base, art: dict[str, Any], texts: dict[str, str]) -> list[dict[str, Any]]:
    th = art["thread"]
    if th.get("parent"):
        parent_line = b.mail_by_mid[th["parent"]]
        refs = list(parent_line["payload"].get("references") or []) + [th["parent"]]
    else:
        refs = []
    parent = th.get("parent")
    out = []
    for m in th["messages"]:
        t = parse_time(m["sent"])
        sender = _addr(b, m["from"])
        dom = sender[1].rpartition("@")[2]
        n = int(m["id"].split("-")[1])
        mid = f"<{t.astimezone(UTC):%Y%m%d%H%M%S}.{n:06d}@{dom}>"
        to = [_addr(b, x) for x in m["to"]]
        cc = [_addr(b, x) for x in m["cc"]]
        payload = {
            "id": m["id"],
            "internetMessageId": mid,
            "conversationId": th["conversation"],
            "subject": th["subject"],
            "from": {"emailAddress": {"name": sender[0], "address": sender[1]}},
            "toRecipients": [{"emailAddress": {"name": x[0], "address": x[1]}} for x in to],
            "ccRecipients": [{"emailAddress": {"name": x[0], "address": x[1]}} for x in cc],
            "sentDateTime": _graph_time(t),
            "receivedDateTime": _graph_time(t + timedelta(seconds=40)),
            "inReplyTo": parent,
            "references": list(refs),
            "importance": "normal",
            "body": {"contentType": "text", "content": texts[m["slot"]]},
            "hasAttachments": False,
            "attachments": [],
            "text_slot": f"mail:{m['id']}:escalation",
        }
        principals = {p.email: p.principal for p in b.people.values()}
        acl = sorted({principals[a] for _, a in [sender, *to, *cc] if a in principals})
        out.append(_line(b.src["email"], "message", mid, t, payload, acl=acl))
        m["message_id"] = mid
        refs.append(mid)
        parent = mid
    return out


# ======================================================================================== sim decision
def decision_line(
    b: Base,
    art: dict[str, Any],
    texts: dict[str, str],
    causes_add: list[dict[str, str]],
    related_add: list[str],
    seq: int,
) -> dict[str, Any]:
    did = art["record"]["id"]
    last = b.sim_line_last("decision", did)
    p = copy.deepcopy(last["payload"])
    t = parse_time(art["publish"])
    p["fields"]["rationale"] = " ".join(texts["rationale"].split())
    p["fields"]["related"] = list(dict.fromkeys([*(p["fields"].get("related") or []), *related_add]))
    owner = p["fields"].get("owner")
    if owner:
        p["fields"]["actor"] = owner
        p["fields"]["actor_name"] = b.people[owner].name
    have = {(c["type"], c["id"]) for c in p.get("causes") or []}
    p["causes"] = list(p.get("causes") or []) + [c for c in causes_add if (c["type"], c["id"]) not in have]
    p["kind"] = "decision_documented"
    p["ts"] = t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    p["seq"] = SIM_SEQ_BASE + seq
    return _line("sim", "decision", did, t, p, container=last["container"], acl=last["acl"])


# ================================================================================== Jira link versions
def link_line(b: Base, key: str, at: str, relationship: str, url: str, title: str, n: int) -> dict[str, Any]:
    it = b.issues[key]
    last = it.lines[-1]
    p = copy.deepcopy(last["payload"])
    t = parse_time(at)
    p["remotelinks"] = list(p.get("remotelinks") or []) + [
        {"id": 9_000_000 + n, "relationship": relationship, "object": {"url": url, "title": title}}
    ]
    p["fields"]["updated"] = _jira_time(b.local(t))
    return _line(b.src["jira"], "issue", key, t, p, container=last["container"])


# ================================================================================================ all
def texts_for(art: dict[str, Any], texts: dict[str, str] | None) -> dict[str, str]:
    pk = Packet.from_json(art["packet"])
    out = {}
    for slot in pk.slots:
        out[slot] = (texts or {}).get(slot) or placeholder_text(pk, slot)
    return out


def render(
    b: Base,
    plan: dict[str, Any],
    texts: dict[str, dict[str, str]] | None = None,
    keep: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Overlay lines and gold links for the artifacts in ``keep`` (all when None).

    ``texts``: artifact id -> slot -> text (placeholders where missing). Links to a page whose
    artifact is not kept are dropped, and so are the causes that would point at it.
    """
    arts = [a for a in plan["artifacts"] if keep is None or a["id"] in keep]
    by_id = {a["id"]: a for a in arts}
    lines: list[dict[str, Any]] = []
    gold: list[dict[str, Any]] = []
    stats = {"pages": 0, "mails": 0, "jira_links": 0, "decisions": 0}

    def g(effect: str, pred: str, cause: str, how: str, since: str, **extra: Any) -> None:
        gold.append(
            {
                "effect": effect,
                "cause": cause,
                "predicate": pred,
                "expressed_as": how,
                "since": since,
                "explicit": not extra,
                **extra,
            }
        )

    link_n = 0
    for a in arts:
        tx_ = texts_for(a, (texts or {}).get(a["id"]))
        if a.get("page"):
            ln = _line(
                b.src["confluence"],
                "page",
                a["page"]["id"],
                parse_time(a["publish"]),
                page_payload(b, a, tx_),
                container=a["page"]["space"],
            )
            lines.append(ln)
            stats["pages"] += 1
            eid = page_entity(a)
            for pr in a["page"]["properties"]:
                if pr["label"] == "Based on":
                    for _, url in pr["links"]:
                        target = url.rsplit("/pages/", 1)[1].split("/")[0]
                        g(eid, "informed_by", f"confluence.page-{target}", "page_property", ln["at"])
            for blk in a["page"]["blocks"]:
                for n, _ in enumerate(blk.get("decisions") or [], start=1):
                    g(f"{eid}.dec-{n}", "decided_in", eid, "decision_item", ln["at"])
                for n, _ in enumerate(blk.get("tasks") or [], start=1):
                    g(f"{eid}.task-{n}", "decided_in", eid, "decision_item", ln["at"])
            for im in a.get("implicit") or []:
                g(eid, im["predicate"], im["cause"], "text", ln["at"], quote=im["quote"])
            for lk in a["links"]:
                link_n += 1
                url = b.page_url(a["page"]["space"], a["page"]["id"], lk.get("frag"))
                title = a["page"]["title"] + (" (decision)" if lk.get("frag") else "")
                jl = link_line(b, lk["key"], lk["at"], lk["relationship"], url, title, link_n)
                lines.append(jl)
                stats["jira_links"] += 1
                pred = "implements" if lk["relationship"] == "implements" else "informed_by"
                g(
                    f"jira.{lk['key']}",
                    pred,
                    eid + (f".{lk['frag']}" if lk.get("frag") else ""),
                    "remote_link",
                    jl["at"],
                )
        elif a.get("thread"):
            tl = thread_lines(b, a, tx_)
            lines += tl
            stats["mails"] += len(tl)
            par = a["thread"].get("parent")
            prev = f"mail.{b.mail_by_mid[par]['payload']['id']}" if par else None
            for ln in tl:
                eid = f"mail.{ln['payload']['id']}"
                if prev:
                    g(eid, "replies_to", prev, "reply", ln["at"])
                prev = eid
            for im in a.get("implicit") or []:
                ln = tl[im["message"]]
                g(
                    f"mail.{ln['payload']['id']}",
                    im["predicate"],
                    im["cause"],
                    "text",
                    ln["at"],
                    quote=im["quote"],
                )
        elif a.get("record"):
            tr = a["trigger"]
            causes: list[dict[str, str]] = []
            related: list[str] = []
            if tr.get("life_event"):
                causes.append({"type": "life_event", "id": tr["life_event"]})
            sync = by_id.get(tr.get("sync") or "")
            if sync:
                causes.append({"type": "habit", "id": page_entity(sync)})
            rv = by_id.get(tr["review"])
            if rv:
                causes.append({"type": "decision", "id": f"{page_entity(rv)}.dec-1"})
                related.append(page_entity(rv))
            ln = decision_line(b, a, tx_, causes, related, stats["decisions"])
            lines.append(ln)
            stats["decisions"] += 1
            have = {
                (c["type"], c["id"])
                for c in b.sim_line_last("decision", a["record"]["id"])["payload"]["causes"]
            }
            for c in causes:
                if (c["type"], c["id"]) in have:
                    continue
                cid = c["id"] if "." in c["id"] else f"sim.{c['id']}"
                g(f"sim.{a['record']['id']}", CAUSE_PREDICATES[c["type"]], cid, "sim_cause", ln["at"])
    return lines, gold, stats
