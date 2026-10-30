"""A small RU/EN corpus for search/ground tests, written through the journal like an adapter would.

Containers and ACLs follow config/sources.yaml. user:jordan.baird (all-staff, product, flowpilot) lacks grp:exec,
grp:atlas and grp:engineering.
"""
from __future__ import annotations

import time
from datetime import datetime

from kb.contracts import AliasSpec, CanonicalRecord, CausalSpec, Ref, StatementSpec
from kb.journal.writer import journal_tx
from kb.worker import framework

PROD = ["grp:all-staff"]
LEAD = ["grp:exec"]
FP = ["grp:exec", "grp:flowpilot", "grp:product"]
EXP = ["grp:exec", "grp:flowpilot", "grp:product"]
ATL = ["grp:atlas", "grp:exec"]
POLICIES = ["grp:all-staff"]
GITLAB = ["grp:engineering", "grp:exec", "grp:flowpilot"]

T_REPORT = "2026-02-27T10:00:00+03:00"
T_SPEC = "2026-03-04T10:00:00+03:00"
T_POLICY_1 = "2026-03-10T10:00:00+03:00"
T_POLICY_2 = "2026-06-15T10:00:00+03:00"
T_RENAME = "2026-07-01T10:00:00+03:00"
T_DRAFT_DELETE = "2026-05-20T10:00:00+03:00"
SEARCH_CONSUMERS = ["search", "descriptors"]


def drain_search(db, tenant: str, timeout: float = 180.0) -> None:
    """drain(consumers=["search", "descriptors"]) until both checkpoints cover the tenant's whole outbox.

    framework.drain only reads rows below pg_snapshot_xmin, so while another session (a parallel test run) holds an
    old snapshot a single drain may legitimately return early; waiting makes the tests deterministic."""
    deadline = time.monotonic() + timeout
    while True:
        framework.drain(db, tenant, consumers=SEARCH_CONSUMERS)
        behind = [c for c in SEARCH_CONSUMERS if db.execute(
            """SELECT EXISTS (SELECT 1 FROM outbox o
                 LEFT JOIN consumer_checkpoint c ON c.consumer = %s AND c.tenant_id = o.tenant_id
                 WHERE o.tenant_id = %s
                   AND (o.xid, o.id) > (COALESCE(c.last_xid, '0'::xid8), COALESCE(c.last_id, 0))) AS behind""",
            (c, tenant)).fetchone()["behind"]]
        if not behind:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"{behind} did not catch up with the outbox of {tenant}")
        time.sleep(0.2)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)


def ref(source: str, otype: str, ext: str) -> Ref:
    return Ref(source_id=source, object_type=otype, external_id=ext)


def mk(source: str, otype: str, ext: str, eid: str, kind: str, title: str, body: str | None, acl: list[str], *,
       keys: tuple[str, ...] = (), fields: dict | None = None, causes: list[CausalSpec] | None = None,
       statements: list[StatementSpec] | None = None, valid_from: datetime | None = None,
       deleted: bool = False) -> CanonicalRecord:
    return CanonicalRecord(
        key=ref(source, otype, ext), entity_id=eid, kind=kind, title=title, body=body, acl=acl, fields=fields or {},
        aliases=[AliasSpec(alias=k, kind="key") for k in keys] + [AliasSpec(alias=title, kind="title")],
        causes=causes or [], statements=statements or [], valid_from=valid_from, deleted=deleted,
        url=f"https://example.test/{eid}",
    )


def apply(db, tenant: str, records: list[CanonicalRecord], when: str, origin: str = "declared:test_import"):
    with journal_tx(db, tenant, "ingest", at=at(when)) as j:
        return j.apply_records(records, origin=origin)


REPORT_BODY = """# Why activation dropped
In February the activation of new teams dropped from 41% to 29%. The main reason is the long onboarding: admins abandon the setup at the calendar connection step.

# What we propose
Shorten the onboarding and show a first-steps checklist."""

SPEC_BODY = """## Goal
New workspaces should reach first value in under ten minutes.

## Scope
A guided checklist walks the admin through connecting a calendar and inviting teammates.

## Metrics
Weekly activation rate and median time to first value."""

POLICY_V1 = "An experiment lasts at most 14 days. The minimum sample is 500 teams per group."
POLICY_V2 = "An experiment lasts at most 21 days. The minimum sample is 1000 teams per group."


def build(db, tenant: str, *, drain: bool = True) -> None:
    apply(db, tenant, [mk("confluence-vf", "page", "310001", "confluence.page-310001", "Report",
                          "February activation report", REPORT_BODY, PROD)], T_REPORT)
    apply(db, tenant, [mk("confluence-vf", "page", "310002", "confluence.page-310002", "Spec",
                          "Onboarding checklist PRD", SPEC_BODY, PROD)], T_SPEC)
    apply(db, tenant, [
        mk("jira-vf", "issue", "FP-101", "jira.FP-101", "Story", "Add a checklist to onboarding",
           "Show new admins a first-steps checklist.", FP, keys=("FP-101",)),
        mk("jira-vf", "issue", "FP-110", "jira.FP-110", "Story", "Checklist analytics",
           "Follow-up of FP-101: checklist step completion events.", FP, keys=("FP-110",),
           statements=[StatementSpec(predicate="relates_to", object=ref("jira-vf", "issue", "FP-101"))]),
        mk("jira-vf", "issue", "FP-120", "jira.FP-120", "Story", "Project Phoenix onboarding",
           "A new first-login flow.", FP, keys=("FP-120",)),
    ], "2026-03-05T10:00:00+03:00", origin="declared:jira_import")
    apply(db, tenant, [mk("xwiki-vf", "page", "Policies.Experiments", "xwiki.policies-experiments", "Policy",
                          "Experiment policy", POLICY_V1, POLICIES, keys=("POL-3",))], T_POLICY_1,
          origin="declared:xwiki_import")
    apply(db, tenant, [
        mk("gitlab-vf", "release", "v2.4.0", "gitlab.release-v2.4.0", "Release", "FlowPilot 2.4.0",
           "Release notes: guided onboarding checklist, calendar connection fixes.", GITLAB,
           keys=("v2.4.0", "flowpilot/app@v2.4.0")),
        mk("gitlab-vf", "merge_request", "42", "gitlab.mr-42", "MergeRequest", "Onboarding checklist UI",
           "Adds the first-steps checklist widget to the onboarding flow.", GITLAB, keys=("!42", "flowpilot/app!42")),
    ], "2026-03-12T10:00:00+03:00", origin="declared:gitlab_import")
    apply(db, tenant, [
        mk("jira-vf", "issue", "EXP-7", "jira.EXP-7", "Experiment", "Checklist onboarding test",
           "We test whether the first-steps checklist raises the activation of new teams. The control group sees the old onboarding.",
           EXP, keys=("EXP-7",),
           causes=[CausalSpec(predicate="based_on", cause=ref("gitlab-vf", "release", "v2.4.0"))]),
        mk("jira-vf", "issue", "EXP-5", "jira.EXP-5", "Experiment", "Price test on the pricing page",
           "We compare two prices of the annual plan.", EXP, keys=("EXP-5",)),
        mk("jira-vf", "issue", "ATL-12", "jira.ATL-12", "Story", "Atlas checklist for ledger migration",
           "Ledger migration checklist for the Atlas team.", ATL, keys=("ATL-12",)),
    ], "2026-03-15T10:00:00+03:00", origin="declared:jira_import")
    apply(db, tenant, [mk("confluence-vf", "page", "310050", "confluence.page-310050", "Meeting",
                          "Product council: activation budget",
                          "The council decided to cut the advertising budget and focus on activating paying teams.",
                          LEAD)], "2026-04-02T10:00:00+03:00", origin="declared:confluence_import")
    apply(db, tenant, [mk("confluence-vf", "page", "310009", "confluence.page-310009", "Document",
                          "Reactivation plan draft", "Send emails with a promo code to inactive teams.", PROD)],
          "2026-04-10T10:00:00+03:00", origin="declared:confluence_import")
    # later history: the policy is tightened, FP-120 renamed, the draft deleted
    apply(db, tenant, [mk("confluence-vf", "page", "310009", "confluence.page-310009", "Document",
                          "Reactivation plan draft", None, PROD, deleted=True)], T_DRAFT_DELETE,
          origin="declared:confluence_import")
    apply(db, tenant, [mk("xwiki-vf", "page", "Policies.Experiments", "xwiki.policies-experiments", "Policy",
                          "Experiment policy", POLICY_V2, POLICIES, keys=("POL-3",))], T_POLICY_2,
          origin="declared:xwiki_import")
    apply(db, tenant, [mk("jira-vf", "issue", "FP-120", "jira.FP-120", "Story", "Guided setup",
                          "A new first-login flow.", FP, keys=("FP-120",), valid_from=at(T_RENAME))], T_RENAME,
          origin="declared:jira_import")
    if drain:
        drain_search(db, tenant)
