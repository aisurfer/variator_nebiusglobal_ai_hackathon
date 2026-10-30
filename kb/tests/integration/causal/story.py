"""The FlowPilot story built by hand through the journal (journal_tx + apply_records), the way adapters would:

report -> Growth sync meeting (held_at, attendees) with decisions DEC-1/DEC-2 (derived records, decided_in)
-> epic FP-101 (implements DEC-1) -> stories FP-102/FP-103 (part_of) -> MRs !42/!43 (closes) -> release v2.4.0
(includes) -> experiment EXP-7 (based_on release, implements DEC-2, governed_by policy, informed_by draft).
Edits: FP-103 description after !43 opened (STALE), epic status only (CURRENT_COMPATIBLE), policy rules (STALE),
DEC-1 status only then statement (COMPATIBLE then STALE), draft deleted (BROKEN), restricted Product council
(grp:exec) whose decision EXP-7@3 is informed by.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kb.contracts import AliasSpec, CanonicalRecord, CausalSpec, Ref
from kb.journal.writer import journal_tx

PROD_ACL = ["grp:all-staff"]
LEAD_ACL = ["grp:exec"]
FP_ACL = ["grp:exec", "grp:flowpilot", "grp:product"]
EXP_ACL = ["grp:exec", "grp:flowpilot", "grp:product"]
GL_ACL = ["grp:engineering", "grp:exec", "grp:flowpilot"]
POL_ACL = ["grp:all-staff"]


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)


# entity ids
REPORT = "confluence.page-300100"
MEETING = "confluence.page-310001"
DEC1 = "confluence.decision-310001-1"
DEC2 = "confluence.decision-310001-2"
EPIC = "jira.FP-101"
ST1 = "jira.FP-102"
ST2 = "jira.FP-103"
MR1 = "gitlab.flowpilot-app.mr-42"
MR2 = "gitlab.flowpilot-app.mr-43"
REL = "gitlab.flowpilot-app.release-v2.4.0"
POLICY = "xwiki.Policies.ExperimentPolicy"
DRAFT = "confluence.page-320500"
EXP = "jira.EXP-7"
COUNCIL = "confluence.page-330001"
DECC = "confluence.decision-330001-1"

# event times (Europe/Moscow)
T = {
    "report": "2026-02-20T10:00:00+03:00",
    "policy": "2026-02-25T10:00:00+03:00",
    "meeting": "2026-03-02T14:00:00+03:00",
    "epic": "2026-03-04T10:00:00+03:00",
    "stories": "2026-03-05T10:00:00+03:00",
    "mrs": "2026-03-20T10:00:00+03:00",
    "story_edit": "2026-03-25T10:00:00+03:00",
    "merged": "2026-04-01T10:00:00+03:00",
    "epic_status": "2026-04-02T10:00:00+03:00",
    "release": "2026-04-10T10:00:00+03:00",
    "draft": "2026-04-12T10:00:00+03:00",
    "exp": "2026-04-15T10:00:00+03:00",
    "exp_running": "2026-04-20T10:00:00+03:00",
    "policy_edit": "2026-05-10T10:00:00+03:00",
    "dec1_status": "2026-05-12T10:00:00+03:00",
    "draft_delete": "2026-05-20T10:00:00+03:00",
    "council": "2026-06-01T18:00:00+03:00",
    "exp_extend": "2026-06-02T10:00:00+03:00",
    "dec1_text": "2026-06-10T10:00:00+03:00",
}


def ext(source: str, otype: str, xid: str) -> Ref:
    return Ref(source_id=source, object_type=otype, external_id=xid)


def acct(source: str, user: str) -> Ref:
    return ext(source, "account", f"712020:{user}" if source in ("jira-vf", "confluence-vf") else user)


def cause(pred: str, ref: Ref, sensitivity: list[str] | None = None, quote: str | None = None) -> CausalSpec:
    return CausalSpec(predicate=pred, cause=ref, sensitivity=sensitivity,
                      evidence={"quote": quote} if quote else None)


R_REPORT = ext("confluence-vf", "page", "300100")
R_MEETING = ext("confluence-vf", "page", "310001")
R_DEC1 = ext("confluence-vf", "decision", "310001#1")
R_DEC2 = ext("confluence-vf", "decision", "310001#2")
R_EPIC = ext("jira-vf", "issue", "FP-101")
R_ST1 = ext("jira-vf", "issue", "FP-102")
R_ST2 = ext("jira-vf", "issue", "FP-103")
R_MR1 = ext("gitlab-vf", "merge_request", "flowpilot/app!42")
R_MR2 = ext("gitlab-vf", "merge_request", "flowpilot/app!43")
R_REL = ext("gitlab-vf", "release", "flowpilot/app@v2.4.0")
R_POLICY = ext("xwiki-vf", "page", "Policies.ExperimentPolicy")
R_DRAFT = ext("confluence-vf", "page", "320500")
R_EXP = ext("jira-vf", "issue", "EXP-7")
R_COUNCIL = ext("confluence-vf", "page", "330001")
R_DECC = ext("confluence-vf", "decision", "330001#1")

ATTENDEES = ["Sofia Lind", "Jordan Baird", "Priya Nand", "Farah Ortega"]


def report() -> CanonicalRecord:
    return CanonicalRecord(
        key=R_REPORT, entity_id=REPORT, kind="Report", title="Activation drop, January cohort",
        fields={"findings": "D7 activation of new workspaces fell from 41% to 33%; users stall at setup.",
                "metrics": {"activation_d7": 0.33}, "period": "2026-01"},
        body="# Findings\nD7 activation fell from 41% to 33%. Users stall at workspace setup.",
        actor=acct("confluence-vf", "harper.vale"), acl=PROD_ACL, aliases=[AliasSpec(alias="Activation drop", kind="title")],
    )


def policy(rules: str, version: str) -> CanonicalRecord:
    return CanonicalRecord(key=R_POLICY, entity_id=POLICY, kind="Policy", title="Experiment policy",
                           fields={"rules": rules, "version": version, "owner": "Priya Nand"},
                           body=f"# Rules\n{rules}", actor=acct("xwiki-vf", "XWiki.PriyaNand"), acl=POL_ACL)


def meeting(body_extra: str = "") -> CanonicalRecord:
    return CanonicalRecord(
        key=R_MEETING, entity_id=MEETING, kind="Meeting", title="Growth sync — 2 March",
        fields={"held_at": "2026-03-02T11:00:00+03:00", "attendees": ATTENDEES, "note_taker": "Jordan Baird"},
        body="# Decisions\nBuild an onboarding checklist. Validate it with an A/B experiment." + body_extra,
        valid_from=at("2026-03-02T11:00:00+03:00"), actor=acct("confluence-vf", "jordan.baird"), acl=PROD_ACL,
        causes=[cause("informed_by", R_REPORT, quote="following the activation report")],
    )


def decision(n: int, statement: str, owner: str, status: str = "accepted") -> CanonicalRecord:
    return CanonicalRecord(
        key=R_DEC1 if n == 1 else R_DEC2, entity_id=DEC1 if n == 1 else DEC2, kind="Decision",
        title=f"DEC-{n}: {statement[:40]}",
        fields={"statement": statement, "owner": owner, "decided_at": "2026-03-02T11:00:00+03:00", "status": status},
        valid_from=at("2026-03-02T11:00:00+03:00"), actor=acct("confluence-vf", "jordan.baird"), acl=PROD_ACL,
        causes=[cause("decided_in", R_MEETING)],
    )


def issue(ref: Ref, eid: str, kind: str, summary: str, description: str, status: str,
          causes: list[CausalSpec] | None = None, acl: list[str] | None = None, extra: dict | None = None) -> CanonicalRecord:
    return CanonicalRecord(
        key=ref, entity_id=eid, kind=kind, title=f"{ref.external_id}: {summary}",
        fields={"summary": summary, "description": description, "status": status} | (extra or {}),
        body=description, actor=acct("jira-vf", "sofia.lind"), acl=acl or FP_ACL, causes=causes or [],
        aliases=[AliasSpec(alias=ref.external_id, kind="key")],
    )


def mr(ref: Ref, eid: str, n: int, story: Ref, state: str, merge_commit: str | None = None) -> CanonicalRecord:
    return CanonicalRecord(
        key=ref, entity_id=eid, kind="MergeRequest", title=f"!{n} checklist part {n - 41}",
        fields={"title": f"Checklist part {n - 41}", "state": state, "merge_commit": merge_commit},
        body=f"Closes {story.external_id}", actor=acct("gitlab-vf", "fortega"), acl=GL_ACL,
        causes=[cause("closes", story, quote=f"Closes {story.external_id}")],
    )


def release() -> CanonicalRecord:
    return CanonicalRecord(
        key=R_REL, entity_id=REL, kind="Release", title="v2.4.0",
        fields={"tag": "v2.4.0", "released_at": "2026-04-10T10:00:00+03:00", "merge_requests": ["!42", "!43"]},
        body="Onboarding checklist.", actor=acct("gitlab-vf", "fortega"), acl=GL_ACL,
        causes=[cause("includes", R_MR1), cause("includes", R_MR2)],
    )


def draft() -> CanonicalRecord:
    return CanonicalRecord(key=R_DRAFT, entity_id=DRAFT, kind="Document", title="EXP-7 design notes (draft)",
                           fields={"findings": "Checklist users complete setup 2x more often (qualitative)."},
                           body="Draft notes.", actor=acct("confluence-vf", "jordan.baird"), acl=PROD_ACL)


def experiment(status: str, duration: int = 14, council: bool = False) -> CanonicalRecord:
    causes = [
        cause("based_on", R_REL, ["tag", "merge_requests"], quote="Experiment on release v2.4.0"),
        cause("implements", R_DEC2),
        cause("governed_by", R_POLICY),
        cause("informed_by", R_DRAFT),
    ]
    if council:
        causes.append(cause("informed_by", R_DECC))
    return issue(R_EXP, EXP, "Experiment", "Onboarding checklist A/B", "A/B test of the onboarding checklist.", status,
                 causes, acl=EXP_ACL,
                 extra={"hypothesis": "The checklist raises D7 activation by 5 pp", "start_date": "2026-04-20",
                        "duration_days": duration, "traffic_split": "50/50", "primary_metric": "activation_d7"})


def council_records() -> list[CanonicalRecord]:
    page = CanonicalRecord(
        key=R_COUNCIL, entity_id=COUNCIL, kind="Meeting", title="Product council — June",
        fields={"held_at": "2026-06-01T15:00:00+03:00", "attendees": ["Mara Voss", "Priya Nand", "Daniel Sato"]},
        body="# Decisions\nExtend the checklist experiment to all new workspaces.", acl=LEAD_ACL,
        valid_from=at("2026-06-01T15:00:00+03:00"), actor=acct("confluence-vf", "priya.nand"),
    )
    dec = CanonicalRecord(
        key=R_DECC, entity_id=DECC, kind="Decision", title="Council: extend EXP-7 to all new workspaces",
        fields={"statement": "Extend EXP-7 to all new workspaces for 4 weeks", "owner": "Priya Nand",
                "decided_at": "2026-06-01T15:00:00+03:00", "status": "accepted"},
        acl=LEAD_ACL, valid_from=at("2026-06-01T15:00:00+03:00"), causes=[cause("decided_in", R_COUNCIL)],
    )
    return [page, dec]


@dataclass
class Story:
    tenant: str
    links: dict[str, str] = field(default_factory=dict)  # name -> link_id
    versions: dict[str, list[str]] = field(default_factory=dict)

    def link(self, name: str) -> str:
        return self.links[name]


def _apply(db, tenant: str, when: str, records: list[CanonicalRecord], origin: str, acl: list[str]) -> Any:
    with journal_tx(db, tenant, "ingest", at=at(when)) as j:
        return j.apply_records(records, origin=origin, default_acl=acl)


DEC1_TEXT = "Build an onboarding checklist in the FlowPilot app"
DEC1_TEXT_V2 = "Build an onboarding checklist with a progress bar in the FlowPilot app"
DEC2_TEXT = "Validate the checklist with an A/B experiment after the release"


def build(db, tenant: str, *, until: str | None = None) -> Story:
    s = Story(tenant)
    stop = at(until) if until else None

    def step(name: str, records: list[CanonicalRecord], origin: str, acl: list[str]) -> Any:
        if stop is not None and at(T[name]) > stop:
            return None
        return _apply(db, tenant, T[name], records, origin, acl)

    conf, jira, gl, xw = ("declared:confluence_import", "declared:jira_import", "declared:gitlab_import",
                          "declared:xwiki_import")
    step("report", [report()], conf, PROD_ACL)
    step("policy", [policy("Run for at least 14 days; at most 50% of traffic.", "1.0")], xw, POL_ACL)
    step("meeting", [meeting(), decision(1, DEC1_TEXT, "Sofia Lind"), decision(2, DEC2_TEXT, "Jordan Baird")],
         conf, PROD_ACL)
    step("epic", [issue(R_EPIC, EPIC, "Epic", "Onboarding checklist", "Checklist that guides workspace setup.", "To Do",
                        [cause("implements", R_DEC1, quote="Implements decision DEC-1")])], jira, FP_ACL)
    step("stories", [issue(R_ST1, ST1, "Story", "Checklist UI", "Panel with setup steps.", "To Do",
                           [cause("part_of", R_EPIC)])], jira, FP_ACL)
    step("stories", [issue(R_ST2, ST2, "Story", "Progress tracking", "Track completed steps.", "To Do",
                           [cause("part_of", R_EPIC)])], jira, FP_ACL)
    step("mrs", [mr(R_MR1, MR1, 42, R_ST1, "opened")], gl, GL_ACL)
    step("mrs", [mr(R_MR2, MR2, 43, R_ST2, "opened")], gl, GL_ACL)
    step("story_edit", [issue(R_ST2, ST2, "Story", "Progress tracking", "Track completed steps per user and team.",
                              "In Progress", [cause("part_of", R_EPIC)])], jira, FP_ACL)
    step("merged", [mr(R_MR1, MR1, 42, R_ST1, "merged", "a1b2c3")], gl, GL_ACL)
    step("merged", [mr(R_MR2, MR2, 43, R_ST2, "merged", "d4e5f6")], gl, GL_ACL)
    step("epic_status", [issue(R_EPIC, EPIC, "Epic", "Onboarding checklist", "Checklist that guides workspace setup.",
                               "In Progress", [cause("implements", R_DEC1, quote="Implements decision DEC-1")])],
         jira, FP_ACL)
    step("release", [release()], gl, GL_ACL)
    step("draft", [draft()], conf, PROD_ACL)
    step("exp", [experiment("Planned")], jira, EXP_ACL)
    step("exp_running", [experiment("Running")], jira, EXP_ACL)
    step("policy_edit", [policy("Run for at least 21 days; at most 30% of traffic; holdout required.", "2.0")],
         xw, POL_ACL)
    step("dec1_status", [meeting(), decision(1, DEC1_TEXT, "Sofia Lind", "in progress"),
                         decision(2, DEC2_TEXT, "Jordan Baird")], conf, PROD_ACL)
    step("draft_delete", [draft().model_copy(update={"deleted": True})], conf, PROD_ACL)
    step("council", council_records(), conf, LEAD_ACL)
    step("exp_extend", [experiment("Running", 28, council=True)], jira, EXP_ACL)
    step("dec1_text", [meeting(" The checklist shows a progress bar."),
                       decision(1, DEC1_TEXT_V2, "Sofia Lind", "in progress"),
                       decision(2, DEC2_TEXT, "Jordan Baird")], conf, PROD_ACL)
    rows = db.execute(
        """SELECT link_id, effect_entity_id, predicate_id, cause_entity_id FROM causal_link
           WHERE tenant_id = %s ORDER BY tx_seq, link_id""", (tenant,)).fetchall()
    names = {MEETING: "meeting", DEC1: "dec1", DEC2: "dec2", EPIC: "epic", ST1: "st1", ST2: "st2", MR1: "mr1",
             MR2: "mr2", REL: "rel", POLICY: "policy", DRAFT: "draft", EXP: "exp", REPORT: "report",
             COUNCIL: "council", DECC: "decc"}
    for r in rows:
        key = f"{names.get(r['effect_entity_id'], r['effect_entity_id'])}->{names.get(r['cause_entity_id'], r['cause_entity_id'])}"
        s.links[key] = r["link_id"]
    return s
