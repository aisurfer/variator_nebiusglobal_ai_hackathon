"""Exporter: tenant config, timeline shape, payloads parsed by the REAL backend adapters, case resolution."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from kbsim import resolve as R
from kbsim.export import (
    Paths, export, md_to_adf_blocks, md_to_jira, read_timeline, split_md_sections,
)
from synth import MSG1, TENANT, world_events, write_tenant

ENTITY_ID = {
    "jira": r"^jira\.[A-Z][A-Z0-9]*-\d+$",
    "confluence": r"^confluence\.page-\d+(\.(dec|task)-\d+)?$",
    "gitlab": r"^gitlab\.[a-z0-9-]+\.(mr-\d+|commit-[0-9a-f]{8})$",
    "email": r"^mail\.[A-Za-z0-9_.@-]+$",
}


def _ctx(paths: Paths, source_id: str):
    from kb.connectors.base import AdapterContext, load_sources

    ts = load_sources(paths.config_dir / "sources.yaml")
    return AdapterContext(TENANT, ts.sources[source_id], ts), ts


def _normalize(paths: Paths, line: dict):
    from kb.connectors.base import get_adapter
    from kb.contracts import SourceEnvelope

    ctx, ts = _ctx(paths, line["source"])
    at = datetime.fromisoformat(line["at"])
    env = SourceEnvelope(tenant_id=TENANT, source_id=line["source"], delivery_id="t", object_type=line["object_type"],
                         external_id=line["external_id"], container=line["container"],
                         provider_version=str(line["version"]) if line.get("version") is not None else None,
                         source_modified_at=datetime.fromisoformat(line["modified_at"]), observed_at=at,
                         payload=line["payload"], acl=line["acl"])
    return get_adapter(ts.sources[line["source"]].provider).normalize(env, ctx)


def _lines(timeline, external_id: str) -> list[dict]:
    return [ln for ln in timeline if ln["external_id"] == external_id]


def _email_adapter() -> bool:
    try:
        from kb.connectors.base import get_adapter

        get_adapter("email")
        return True
    except LookupError:
        return False


# ------------------------------------------------------------------------------------------------ config
def test_sources_yaml(exported):
    from kb.connectors.base import load_sources

    paths, _ = exported
    ts = load_sources(paths.config_dir / "sources.yaml")
    assert (ts.tenant_id, ts.timezone) == (TENANT, "Asia/Bangkok")
    assert {s.id: s.provider for s in ts.sources.values()} == {  # no XWiki source: no XWiki connector in this copy
        "jira-simt": "jira", "confluence-simt": "confluence", "gitlab-simt": "gitlab",
        "mail-simt": "email", "directory-simt": "directory"}
    assert ts.sources["jira-simt"].base_url == "https://simt.atlassian.net"
    assert ts.sources["confluence-simt"].base_url == "https://simt.atlassian.net/wiki"
    assert ts.sources["gitlab-simt"].base_url == "https://git.test.digital"
    assert ts.sources["mail-simt"].base_url is None and not ts.sources["mail-simt"].containers
    assert ts.sources["gitlab-simt"].raw["jira_source"] == "jira-simt"
    assert ts.sources["gitlab-simt"].raw["jira_keys"] == ["MENU", "HR"]
    assert ts.sources["jira-simt"].container_acl("HR") == ["grp:hr", "grp:management"]
    assert ts.sources["directory-simt"].container_acl("people") == ["grp:all-staff"]
    raw = yaml.safe_load((paths.config_dir / "sources.yaml").read_text(encoding="utf-8"))
    first = min(datetime.fromisoformat(x["at"]) for x in read_timeline(paths.timeline))
    assert datetime.fromisoformat(raw["tenant"]["people_at"]) < first


def test_people_yaml(exported):
    paths, _ = exported
    p = yaml.safe_load((paths.config_dir / "people.yaml").read_text(encoding="utf-8"))
    assert p["admins"] == ["user:admin"]
    assert p["groups"]["grp:all-staff"]["name"] == "All staff"
    assert p["groups"]["grp:management"]["name"] == "Management" and p["groups"]["grp:hr"]["name"] == "People operations"
    assert {"grp:sales", "grp:hr"} <= set(p["groups"])  # mailbox and container groups too
    ceo = p["people"][0]
    assert list(ceo) == ["id", "name", "role", "department", "email", "principal", "groups", "accounts"]
    assert ceo["id"] == "KD-E01" and ceo["principal"] == "user:a.sokolov"
    assert ceo["accounts"]["mail-simt"] == "a.sokolov@test.digital"  # lower-cased from "A.Sokolov@..."
    assert ceo["accounts"]["jira-simt"] == "712020:a.sokolov"
    dev = p["people"][2]
    assert dev["accounts"]["gitlab-simt"] == "ipetrov" and "xwiki-simt" not in dev["accounts"]
    orgs = {o["id"]: o for o in p["organizations"]}
    assert list(orgs["org-translog"]) == ["id", "name", "kind", "domain", "inn"]
    assert "org-simt" in orgs  # our own company: counterparty of outgoing documents
    c = p["contacts"][0]
    assert c == {"id": "CT-tl_ivanov", "name": "Peter Ivanov", "role": "Head of Logistics", "org": "org-translog",
                 "email": "P.Ivanov@translog.com", "accounts": {"mail-simt": "p.ivanov@translog.com"}}


# ------------------------------------------------------------------------------------------------ timeline
def test_timeline_shape(exported, timeline):
    _, report = exported
    assert report["failed_events"] == [] and report["adapters"]["error_count"] == 0
    wiki = [e for e in world_events() if e["kind"].startswith("wiki.")]
    assert len(timeline) == len(world_events()) - len(wiki)  # wiki pages are skipped
    ats = [datetime.fromisoformat(x["at"]) for x in timeline]
    assert ats == sorted(ats), "at must be non-decreasing"
    for ln in timeline:
        assert list(ln)[:8] == ["at", "source", "object_type", "external_id", "container", "op", "modified_at", "acl"]
        assert ln["at"].endswith("+07:00") and ln["modified_at"].endswith("+07:00")
        assert datetime.fromisoformat(ln["at"]) >= datetime.fromisoformat(ln["modified_at"])
        assert ln["op"] == "upsert" and isinstance(ln["payload"], dict)
        assert (ln["acl"] is not None) == (ln["source"] == "mail-simt")
    kinds = {(x["source"], x["object_type"]) for x in timeline}
    assert kinds == {("jira-simt", "issue"), ("confluence-simt", "page"), ("gitlab-simt", "merge_request"),
                     ("gitlab-simt", "commit"), ("mail-simt", "message")}
    ids = {x["external_id"] for x in timeline}
    assert {"MENU-1", "600001", "test/menu-backend!1", MSG1} <= ids and "Regulations.PaymentPolicy" not in ids
    assert any(re.fullmatch(r"test/menu-backend@[0-9a-f]{40}", i) for i in ids)


def test_every_payload_normalizes_with_the_real_adapters(exported, timeline):
    paths, _ = exported
    for ln in timeline:
        if ln["source"] == "mail-simt" and not _email_adapter():
            continue
        recs = _normalize(paths, ln)
        provider = ln["source"].split("-")[0].replace("mail", "email")
        assert recs and not recs[0].deleted
        for r in recs:
            assert re.match(ENTITY_ID[provider], r.entity_id), r.entity_id


# ------------------------------------------------------------------------------------------------ Jira
def test_jira_issue_versions(exported, timeline):
    paths, _ = exported
    v = _lines(timeline, "MENU-1")
    assert len(v) == 3  # created, commented, updated
    p = v[-1]["payload"]
    f = p["fields"]
    assert f["description"].startswith("h2. Goal") and "** sub-step" in f["description"]
    assert f["status"] == {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}
    assert f["assignee"]["accountId"] == "712020:i.petrov"
    assert [c["author"]["displayName"] for c in f["comment"]["comments"]] == ["Olga Morozova", "Igor Petrov"]
    assert f["created"] == "2026-06-15T10:05:00.000+0700"
    link = f["issuelinks"][0]
    assert link["type"]["name"] == "Relates" and link["outwardIssue"]["key"] == "MENU-2"
    assert [r["relationship"] for r in p["remotelinks"]] == ["implements"]  # the e-mail target has no URL: dropped
    assert p["remotelinks"][0]["object"]["url"] == "https://simt.atlassian.net/wiki/spaces/GEN/pages/600001#dec-1"
    assert p["changelog"]["histories"][0]["items"][0]["field"] == "status"
    rec = _normalize(paths, v[-1])[0]
    causes = {(c.predicate, c.cause.external_key()) for c in rec.causes}
    assert ("implements", "confluence-simt/decision/600001#dec-1") in causes
    assert rec.fields["comments"][0]["body"].startswith("A 3.5% fee")
    issue_type = _lines(timeline, "MENU-2")[0]["payload"]["fields"]["issuetype"]["name"]
    assert issue_type == "Task"  # "task" -> the Jira name


# ------------------------------------------------------------------------------------------------ Confluence
def test_confluence_meeting_page(exported, timeline):
    paths, _ = exported
    v1, v2 = _lines(timeline, "600001")
    p = v1["payload"]
    assert [x["name"] for x in p["labels"]["results"]] == ["meeting-notes"]
    assert (v1["version"], v2["version"]) == (1, 2) and v2["payload"]["version"]["number"] == 2
    doc = json.loads(p["body"]["atlas_doc_format"]["value"])
    assert doc["content"][0]["attrs"]["extensionKey"] == "details"
    heads = [n["content"][0]["text"] for n in doc["content"] if n["type"] == "heading"]
    assert heads.count("Decisions") == 1  # the markdown "## Decisions" duplicate was replaced by the decision list
    dl = next(n for n in doc["content"] if n["type"] == "decisionList")
    assert [d["attrs"]["localId"] for d in dl["content"]] == ["dec-1", "dec-2"]
    tl = next(n for n in doc["content"] if n["type"] == "taskList")
    assert tl["content"][0]["content"][0] == {"type": "mention", "attrs": {"id": "712020:i.petrov",
                                                                          "text": "@Igor Petrov", "accessLevel": ""}}
    recs = {r.entity_id: r for r in _normalize(paths, v1)}
    page = recs["confluence.page-600001"]
    assert page.kind == "Meeting"
    assert page.fields["held_at"] == "2026-06-15T15:00:00+07:00" and page.fields["ends_at"] is None
    assert page.fields["attendees"] == ["Andrew Sokolov", "Olga Morozova", "Igor Petrov"]
    assert page.fields["owner"] == "Andrew Sokolov"
    assert ("informed_by", "jira-simt/issue/MENU-1") in {(c.predicate, c.cause.external_key()) for c in page.causes}
    assert "| PayNova | 3.5% |" in page.body  # markdown table -> ADF table
    assert recs["confluence.page-600001.dec-1"].fields["statement"] == "We connect PayNova for online payment"
    task = recs["confluence.page-600001.task-1"]
    assert [s.object.external_key() for s in task.statements if s.predicate == "assignee"] == \
        ["confluence-simt/account/712020:i.petrov"]
    later = {r.entity_id: r for r in _normalize(paths, v2)}
    assert later["confluence.page-600001.dec-1"].fields["statement"].startswith("We drop PayNova")


# ------------------------------------------------------------------------------------------------ wiki / GitLab
def test_wiki_pages_are_skipped(exported, timeline):
    """This copy has no XWiki connector: wiki pages of the world are counted and skipped, never delivered."""
    _, report = exported
    assert report["by_source"]["skipped_wiki"] == 2
    assert not [x for x in timeline if x["source"].startswith("xwiki")]
    assert any("no XWiki connector" in w for w in report["warnings"])


def test_gitlab_mr_and_commit(exported, timeline, resolved):
    paths, _ = exported
    mr = _lines(timeline, "test/menu-backend!1")
    assert [x["payload"]["state"] for x in mr] == ["opened", "opened", "merged"]
    last = mr[-1]["payload"]
    assert last["description"].endswith("Closes MENU-1") and last["merged_by"]["username"] == "a.sokolov"
    assert last["reviewers"][0]["username"] == "a.sokolov" and last["notes"][0]["body"].startswith("OK")
    rec = _normalize(paths, mr[-1])[0]
    assert rec.entity_id == "gitlab.test-menu-backend.mr-1" and rec.fields["state"] == "merged"
    assert ("closes", "jira-simt/issue/MENU-1") in {(c.predicate, c.cause.external_key()) for c in rec.causes}
    # the review comment does not change the record (GitLab adapter ignores notes): merge = version 2
    assert resolved["beats"]["MR-02"]["versions"] == ["gitlab.test-menu-backend.mr-1@3"]
    commit = next(x for x in timeline if x["object_type"] == "commit")
    crec = _normalize(paths, commit)[0]
    assert crec.entity_id == "gitlab.test-menu-backend.commit-3f1c2b9a"


# ------------------------------------------------------------------------------------------------ mail
def test_email_payloads(timeline):
    msgs = [x for x in timeline if x["source"] == "mail-simt"]
    first, reply, secret = msgs
    assert first["object_type"] == "message" and first["external_id"] == MSG1 and first["container"] is None
    p = first["payload"]
    assert list(p) == ["id", "internetMessageId", "conversationId", "subject", "from", "toRecipients", "ccRecipients",
                       "sentDateTime", "receivedDateTime", "inReplyTo", "references", "importance", "body",
                       "hasAttachments", "attachments"]
    assert p["from"] == {"emailAddress": {"name": "Peter Ivanov", "address": "P.Ivanov@translog.com"}}
    assert p["ccRecipients"] == [{"emailAddress": {"name": "info", "address": "info@test.digital"}}]
    assert p["sentDateTime"] == "2026-06-16T03:00:00Z" and p["body"]["contentType"] == "text"
    att = p["attachments"][0]
    assert list(att) == ["id", "name", "contentType", "size", "extractedText", "documentMeta"]
    assert att["id"] == "att-1" and "1,500,000" in att["extractedText"]
    assert att["documentMeta"] == {"docType": "contract", "title": "Service agreement No. 14/2026",
                                   "number": "14/2026", "date": "2026-06-16", "counterparty": "org-translog",
                                   "amount": 1500000.0, "currency": "USD"}
    assert first["acl"] == ["grp:sales", "user:m.lebedeva"]  # Email.mailbox_acl as computed by the generator
    assert reply["payload"]["inReplyTo"] == MSG1 and reply["payload"]["references"] == [MSG1]
    assert secret["acl"] == ["user:a.sokolov", "user:o.morozova"]


def test_email_adapter_ids_match_the_resolver(exported, timeline):
    if not _email_adapter():
        pytest.skip("email adapter (kb.connectors.email) not available yet; payloads are written to spec")
    paths, _ = exported
    first = next(x for x in timeline if x["external_id"] == MSG1)
    recs = _normalize(paths, first)
    assert [r.entity_id for r in recs] == [R.mail_entity("msg-000001"), R.attachment_entity("msg-000001", "att-1")]
    assert recs[1].kind == "Contract" and recs[1].acl == ["grp:sales", "user:m.lebedeva"]
    reply = _normalize(paths, next(x for x in timeline if x["payload"].get("inReplyTo") == MSG1))[0]
    assert ("replies_to", f"mail-simt/message/{MSG1}") in {(c.predicate, c.cause.external_key()) for c in reply.causes}


# ------------------------------------------------------------------------------------------------ resolution
def test_cases_resolved(resolved):
    cases = {c["id"]: c for c in resolved["cases"]}
    # GHOST-01 has no events; REG-01/REG-02 are wiki pages, which this copy does not export (no XWiki connector)
    assert resolved["stats"] == {"cases": 7, "skipped": 1, "beats": 13, "beats_realized": 10, "beats_unrealized": 3}
    assert [resolved["beats"][b]["reason"] for b in ("REG-01", "REG-02")] == ["no artefact", "no artefact"]
    c1 = cases["C001"]["evidence"][0]
    assert c1["entities"] == ["mail.msg-000001", "mail.msg-000001.att-1"]
    assert c1["versions"] == ["mail.msg-000001@1", "mail.msg-000001.att-1@1"]
    c2 = cases["C002"]
    assert c2["evidence"][0]["entities"] == ["jira.MENU-1"] and c2["evidence"][0]["versions"] == ["jira.MENU-1@3"]
    root = c2["roots"][0]
    assert root["primary"] == "confluence.page-600001.dec-1"  # decision beat -> the matching decision item
    assert "confluence.page-600001.dec-2" not in root["entities"]
    assert cases["C003"]["forbidden"][0]["all"] == ["mail.msg-000003"]
    assert cases["C003"]["forbidden"][0]["facts"] == [{"key": "salary", "any": ["250,000"]}]
    rev = cases["C006"]["evidence"][1]
    assert rev["beat"] == "PAY-05" and rev["primary"] == "confluence.page-600001.dec-1"
    assert rev["versions"] == ["confluence.page-600001@2", "confluence.page-600001.dec-1@2"]
    c5 = cases["C005"]
    assert c5["skip"] and c5["unrealized"] == ["GHOST-01"] and c5["evidence"][0]["reason"] == "no events"
    assert cases["C007"]["known_at"] == "2026-06-16T18:00:00+07:00"
    assert resolved["beats"]["PAY-03"]["versions"] == ["jira.MENU-1@2"]


def test_conventions_match_the_adapters(tmp_path, resolved):
    """Without the backend (--no-validate) the resolver must produce the same entity and version ids."""
    paths = write_tenant(tmp_path)
    export(paths, validate=False)
    naive = json.loads(paths.resolved.read_text(encoding="utf-8"))
    for bid, b in resolved["beats"].items():
        nb = naive["beats"][bid]
        assert (nb["entities"], nb["versions"], nb["primary"]) == (b["entities"], b["versions"], b["primary"]), bid


# ------------------------------------------------------------------------------------------------ robustness
def test_handles_in_prose_are_humanized_and_broken_events_reported(tmp_path):
    world = world_events()
    world[4]["data"]["comment"]["body"] = "see ticket:pay-integration and page:pay-meeting, and doc:contract-translog"
    world.append({**world[4], "seq": len(world) + 1, "artifact_id": "MENU-404", "beat_id": None})
    paths = write_tenant(tmp_path, world=world)
    rep = export(paths)
    assert rep["failed_events"] == [len(world)]
    tl = read_timeline(paths.timeline)
    body = _lines(tl, "MENU-1")[1]["payload"]["fields"]["comment"]["comments"][0]["body"]
    assert body == 'see MENU-1 and "Payment meeting June 15", and "Service agreement No. 14/2026"'


def test_page_numbers_are_deterministic():
    n = R.PageNumbers()
    assert [n("page-0042"), n("page-0001"), n("page-0042")] == ["600042", "600001", "600042"]
    odd = n("meeting-notes-x")
    assert odd.startswith("7") and R.PageNumbers()("meeting-notes-x") == odd


def test_mail_entity_slug():
    assert R.mail_entity("msg-000123") == "mail.msg-000123"
    assert R.mail_entity("<20260616.0931@company.example>") == "mail.20260616.0931@company.example"
    assert R.attachment_entity("a b", "att 1") == "mail.a-b.att-1"
    assert R.entity_of("mail.x@company.example@3") == "mail.x@company.example"
    assert R.entity_of("mail.x@company.example") == "mail.x@company.example"


# ------------------------------------------------------------------------------------------------ markup
def test_markdown_to_adf():
    blocks = md_to_adf_blocks("# Heading\n\nText **bold** and [link](https://x.example), `code`\nsecond line\n\n"
                              "- a\n  - b\n- c\n\n1. one\n2. two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
                              "```python\nx = 1\n```\n\n> quote\n\n---\nhttps://wiki.test.digital/bin/view/A/B/.")
    types = [b["type"] for b in blocks]
    assert types == ["heading", "paragraph", "bulletList", "orderedList", "table", "codeBlock", "blockquote", "rule",
                     "paragraph"]
    para = blocks[1]["content"]
    assert {"type": "text", "text": "bold", "marks": [{"type": "strong"}]} in para
    assert {"type": "text", "text": "link", "marks": [{"type": "link", "attrs": {"href": "https://x.example"}}]} in para
    assert {"type": "hardBreak"} in para
    nested = blocks[2]["content"][0]["content"]
    assert nested[1]["type"] == "bulletList"
    assert blocks[4]["content"][0]["content"][0]["type"] == "tableHeader"
    assert blocks[8]["content"][0]["marks"][0]["attrs"]["href"] == "https://wiki.test.digital/bin/view/A/B/"
    assert blocks[8]["content"][1]["text"] == "."


def test_markdown_to_jira():
    md = "## Steps\n- **important** [doc](https://x.example)\n  - nested\n1. first\n\n| A | B |\n|---|---|\n| 1 | 2 |\n`code`"
    assert md_to_jira(md) == ("h2. Steps\n* *important* [doc|https://x.example]\n** nested\n# first\n\n||A||B||\n|1|2|\n"
                              "{{code}}")


def test_split_md_sections():
    body, items = split_md_sections("## Discussion\ntext\n## Decisions:\n- one\n- two\n### details\nx\n## Outcome\no",
                                    {"decisions"})
    assert items == ["one", "two"] and body == "## Discussion\ntext\n## Outcome\no"
