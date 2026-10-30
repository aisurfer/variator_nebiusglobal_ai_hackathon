"""Query planner + executor (Docs/KB/QUERY-PLANNER.md) on a small team built through the journal: lists over any
kind with time windows, relations (participants, authors, an organisation standing for its people), the internal
group, state transitions, nested patterns, known_at, ACL, empty windows, the LLM planner with validation, retry,
cache and the fallback to rules."""
from __future__ import annotations

from datetime import datetime

import pytest

from kb.acl import resolve_principal
from kb.contracts import CanonicalRecord, CausalSpec, Ref, StatementSpec
from kb.journal.writer import journal_tx
from kb.reasoning import plan as P
from kb.reasoning.ask import ask

pytestmark = pytest.mark.integration
NOW = datetime.fromisoformat("2026-09-22T12:00:00+03:00")  # Tuesday
ALL = ["grp:all-staff"]
EXEC = ["grp:exec"]
MARA, JOEL, PRIYA = "person.vf-e001", "person.vf-e002", "person.vf-e003"
LENA, ORG = "person.ct-hb1", "org.harbor"


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)


def ref(otype: str, xid: str, source: str = "confluence-vf") -> Ref:
    return Ref(source_id=source, object_type=otype, external_id=xid)


def person(eid: str) -> Ref:
    return Ref(entity_id=eid)


def meeting(xid: str, title: str, held: str, *, participants=(), mentions=(), author: str | None = None,
            acl=ALL) -> CanonicalRecord:
    st = [StatementSpec(predicate="participant", object=person(p)) for p in participants]
    st += [StatementSpec(predicate="mentions", object=person(p)) for p in mentions]
    if author:
        st.append(StatementSpec(predicate="author", object=person(author)))
    return CanonicalRecord(key=ref("page", xid), entity_id=f"confluence.page-{xid}", kind="Meeting", title=title,
                           fields={"held_at": held}, body=f"# {title}\nNotes.", valid_from=at(held), statements=st, acl=acl)


def email(xid: str, title: str, sent: str, author: str) -> CanonicalRecord:
    return CanonicalRecord(key=ref("message", xid, "mail-vf"), entity_id=f"mail.{xid}", kind="Email", title=title,
                           fields={"sent_at": sent}, body=title, valid_from=at(sent),
                           statements=[StatementSpec(predicate="author", object=person(author))], acl=ALL)


def task(key: str, title: str, status: str, when: str) -> CanonicalRecord:
    return CanonicalRecord(key=ref("issue", key, "jira-vf"), entity_id=f"jira.{key}", kind="Task", title=title,
                           fields={"status": status, "summary": title}, body=title, valid_from=at(when), acl=ALL)


def decision(xid: str, title: str, when: str, meeting_xid: str) -> CanonicalRecord:
    return CanonicalRecord(key=ref("decision", xid), entity_id=f"confluence.decision-{xid}", kind="Decision",
                           title=title, fields={"statement": title, "decided_at": when}, valid_from=at(when),
                           causes=[CausalSpec(predicate="decided_in", cause=ref("page", meeting_xid))], acl=ALL)


def apply(db, tenant: str, when: str, records: list[CanonicalRecord], origin: str = "declared:confluence_import") -> None:
    with journal_tx(db, tenant, "ingest", at=at(when)) as j:
        j.apply_records(records, origin=origin, default_acl=ALL)


@pytest.fixture
def team(db, tenant):
    apply(db, tenant, "2026-08-01T09:00:00+03:00", [
        CanonicalRecord(key=ref("organization", "harbor", "directory"), entity_id=ORG, kind="Organization",
                        title="Harbor Foods", fields={"kind": "client"}, acl=ALL),
        CanonicalRecord(key=ref("contact", "hb1", "directory"), entity_id=LENA, kind="Person", title="Lena Brook",
                        fields={"external": True, "org": "harbor"}, acl=ALL,
                        statements=[StatementSpec(predicate="works_at", object=Ref(entity_id=ORG))]),
    ], origin="declared:directory_import")
    apply(db, tenant, "2026-08-02T10:00:00+03:00", [task("FP-901", "Old task", "To Do", "2026-08-02T10:00:00+03:00")],
          "declared:jira_import")
    apply(db, tenant, "2026-08-10T10:00:00+03:00", [email("m-1", "Harbor: pilot terms", "2026-08-10T10:00:00+03:00", LENA)],
          "declared:email_import")
    apply(db, tenant, "2026-08-12T10:00:00+03:00", [email("m-2", "Internal: pilot plan", "2026-08-12T10:00:00+03:00", MARA)],
          "declared:email_import")
    apply(db, tenant, "2026-08-20T10:00:00+03:00", [task("FP-901", "Old task", "Done", "2026-08-20T10:00:00+03:00")],
          "declared:jira_import")
    apply(db, tenant, "2026-08-25T10:00:00+03:00", [
        meeting("900", "Team sync — 25 Aug", "2026-08-25T10:00:00+03:00", participants=[MARA, JOEL]),
        decision("900#1", "Freeze the scope of the pilot", "2026-08-25T10:00:00+03:00", "900")])
    apply(db, tenant, "2026-09-05T10:00:00+03:00", [email("m-3", "Harbor: invoice question", "2026-09-05T10:00:00+03:00", LENA)],
          "declared:email_import")
    apply(db, tenant, "2026-09-10T10:00:00+03:00", [task("FP-902", "Pilot checklist", "To Do", "2026-09-10T10:00:00+03:00"),
                                                    task("FP-903", "Pilot report", "In Progress", "2026-09-10T10:00:00+03:00")],
          "declared:jira_import")
    apply(db, tenant, "2026-09-15T10:00:00+03:00", [task("FP-902", "Pilot checklist", "Done", "2026-09-15T10:00:00+03:00")],
          "declared:jira_import")
    apply(db, tenant, "2026-09-17T10:00:00+03:00", [
        meeting("901", "Team sync — 17 Sep", "2026-09-17T10:00:00+03:00", participants=[MARA, JOEL]),
        decision("901#1", "Ship the pilot checklist", "2026-09-17T10:00:00+03:00", "901")])
    apply(db, tenant, "2026-09-18T10:00:00+03:00", [
        meeting("902", "Call with Harbor Foods — 18 Sep", "2026-09-18T10:00:00+03:00", participants=[MARA, LENA])])
    apply(db, tenant, "2026-09-19T10:00:00+03:00", [
        meeting("903", "Planning — 19 Sep", "2026-09-19T10:00:00+03:00", mentions=[LENA], author=JOEL),
        meeting("904", "Retro — 19 Sep", "2026-09-19T15:00:00+03:00", mentions=[PRIYA], author=MARA),
        meeting("905", "Exec offsite — 19 Sep", "2026-09-19T16:00:00+03:00", participants=[MARA], acl=EXEC)])
    # notes of a meeting held on the 19th, written down only on the 21st (recorded after a known_at of the 20th)
    apply(db, tenant, "2026-09-21T10:00:00+03:00", [
        meeting("906", "Design review — 19 Sep", "2026-09-19T17:00:00+03:00", participants=[PRIYA, JOEL])])
    return tenant


def _ask(db, tenant, q, principal="user:admin", now=NOW, **kw):
    p = resolve_principal(db, tenant, principal)
    return ask(db, p, q, now=now, mode="deterministic", **kw)


def _ids(out) -> list[str]:
    return [i["entity_id"] for i in out["results"]["list"]["items"]]


def test_internal_meetings_of_the_last_week(db, team):
    out = _ask(db, team, "which meetings, syncs and calls did we hold within the team over the past week?")
    plan = out["plan"]
    assert plan["operation"] == "list" and plan["kinds"] == ["Meeting"]
    assert plan["time"]["start"].startswith("2026-09-16T00:00") and plan["time"]["end"].startswith("2026-09-23T00:00")
    assert {"src": "x", "predicate": "participant", "dst": "g_ext", "negate": True} in plan["edges"]
    # participants recorded (901, 906); no participant list -> author and mentions (904); 902 and 903 have Lena
    assert set(_ids(out)) == {"confluence.page-901", "confluence.page-904", "confluence.page-906",
                              "confluence.page-905"}
    assert out["results"]["list"]["total"] == 4
    assert "Found: 4" in out["answer"] and "[confluence.page-901@1]" in out["answer"]


def test_relations_follow_the_knowledge_interval_not_the_write_time(db, team):
    """A fact derived later (a rederive, a late extraction) is known from the moment it was derived from: its row is
    written after K on the transaction axis but its `known` interval contains K. Lists on K must see it."""
    k = datetime.fromisoformat("2026-09-20T00:00:00+03:00")
    later = datetime.fromisoformat("2026-09-21T12:00:00+03:00")
    # Lena's mention in the planning notes of the 19th, as a fact written on the 21st but known since the 19th
    n = db.execute(
        """UPDATE statement SET tx = tstzrange(%s, NULL, '[)'), known = tstzrange(lower(known), NULL, '[)')
           WHERE tenant_id = %s AND subject_id = 'confluence.page-903' AND object_id = %s AND upper_inf(tx)""",
        (later, team, LENA)).rowcount
    assert n == 1
    out = _ask(db, team, "which meetings, syncs and calls did we hold within the team over the past week?", known_at=k)
    assert "confluence.page-903" not in _ids(out)  # Lena, an external contact, is in it as known on the 20th


def test_acl_applies_to_lists(db, team):
    out = _ask(db, team, "Which meetings were held within the team over the past week?", principal="user:jordan.baird")
    assert "confluence.page-905" not in _ids(out)  # grp:exec only


def test_an_organisation_stands_for_its_people(db, team):
    out = _ask(db, team, "Which meetings were held with Harbor Foods over the past week?")
    assert set(_ids(out)) == {"confluence.page-902", "confluence.page-903"}


def test_letters_from_a_person_in_a_month(db, team):
    out = _ask(db, team, "emails from Lena Brook in August")
    assert out["plan"]["operation"] == "list" and _ids(out) == ["mail.m-1"]
    out = _ask(db, team, "emails from Harbor Foods in September")
    assert _ids(out) == ["mail.m-3"]


def test_tasks_closed_in_the_last_days(db, team):
    out = _ask(db, team, "Which tasks were closed in the last 10 days?")
    assert out["plan"]["time"]["event"] == "state" and out["plan"]["time"]["state_values"] == ["Done"]
    assert _ids(out) == ["jira.FP-902"]
    item = out["results"]["list"]["items"][0]
    assert item["event_at"].isoformat().startswith("2026-09-15")  # the date of the transition, not of creation


def test_decisions_at_meetings_with_a_person(db, team):
    out = _ask(db, team, "decisions made at meetings attended by Joel Mercer in September")
    edges = out["plan"]["edges"]
    assert {"src": "x", "predicate": "decided_in", "dst": "m1", "negate": False} in edges
    assert _ids(out) == ["confluence.decision-901#1"]


def test_count(db, team):
    out = _ask(db, team, "How many meetings were there over the past week?")
    assert out["plan"]["operation"] == "count" and out["results"]["list"]["total"] == 6
    assert "Found: 6" in out["answer"]


def test_known_at_cuts_later_records(db, team):
    k = datetime.fromisoformat("2026-09-20T00:00:00+03:00")
    out = _ask(db, team, "Which meetings were held over the past week?", known_at=k)
    assert "confluence.page-906" not in _ids(out) and "confluence.page-904" in _ids(out)
    out = _ask(db, team, "Which meetings were held over the past week?")
    assert "confluence.page-906" in _ids(out)


def test_empty_window_says_where_data_is(db, team):
    out = _ask(db, team, "Which meetings were held over the past week?", now=datetime.fromisoformat("2026-10-26T12:00:00+03:00"))
    assert out["results"]["list"]["total"] == 0
    assert "2026-10-20 — 2026-10-26" in out["answer"]
    assert "ends on 2026-09-19" in out["answer"]
    assert out["unknowns"]


def test_english_list(db, team):
    out = _ask(db, team, "Which meetings did the team hold internally over the past week?")
    assert out["language"] == "en" and out["plan"]["operation"] == "list"
    assert set(_ids(out)) == {"confluence.page-901", "confluence.page-904", "confluence.page-906", "confluence.page-905"}
    assert "Found: 4" in out["answer"]


def test_legacy_operations_still_route(db, team):
    out = _ask(db, team, "Why was the decision Ship the pilot checklist made?")
    assert out["intent"] == "why" and out["plan"]["operation"] == "why"


# ------------------------------------------------------------------------------------------------ LLM planner
class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

        class U:
            def as_dict(self_inner):
                return {}
        self.usage = U()

    def json(self, *, system, user, schema, name, effort="low", max_output_tokens=2000):
        self.calls.append(name)
        if name != "query_plan":
            raise RuntimeError("only planning in this test")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _llm_plan(**over):
    base = {"operation": "list", "kinds": ["Meeting"],
            "nodes": [{"id": "g", "kinds": ["Person"], "text": None, "key": None, "group": "external"}],
            "edges": [{"src": "x", "predicate": "participant", "dst": "g", "negate": True}],
            "time": {"start": "2026-09-16T00:00:00+03:00", "end": "2026-09-23T00:00:00+03:00", "applies_to": "x",
                     "event": "occurred", "state_field": None, "state_values": [], "known_at": None,
                     "expression": "over the past seven nights", "interpretation": "from 16.09 to 22.09 inclusive"},
            "text": "", "text_filter": False, "subject": None, "hypothetical": None, "fields": [], "sort": "time_desc",
            "limit": 50}
    base.update(over)
    return base


def test_llm_plan_with_retry_and_cache(db, team, monkeypatch):
    P.clear_cache()
    bad = _llm_plan(edges=[{"src": "x", "predicate": "attended", "dst": "g", "negate": True}])
    fake = FakeLLM([bad, _llm_plan()])
    monkeypatch.setattr("kb.reasoning.ask.get_llm", lambda budget=None: fake)
    p = resolve_principal(db, team, "user:admin")
    q = "who met without outsiders over the past seven nights?"  # the rules do not know this period
    out = ask(db, p, q, now=NOW, mode="auto")
    assert fake.calls.count("query_plan") == 2  # invalid -> one retry with the errors
    assert out["plan"]["planner"] == "llm" and not out["plan"]["cached"]
    assert set(_ids(out)) == {"confluence.page-901", "confluence.page-904", "confluence.page-906", "confluence.page-905"}
    assert "from 16.09 to 22.09" in out["plan"]["time"]["interpretation"]
    out = ask(db, p, q, now=NOW, mode="auto")
    assert fake.calls.count("query_plan") == 2 and out["plan"]["cached"]


def test_llm_failure_falls_back_to_rules(db, team, monkeypatch):
    P.clear_cache()
    fake = FakeLLM([RuntimeError("provider down")])
    monkeypatch.setattr("kb.reasoning.ask.get_llm", lambda budget=None: fake)
    p = resolve_principal(db, team, "user:admin")
    out = ask(db, p, "Which meetings were held within the team over the past week?", now=NOW, mode="auto")
    assert out["plan"]["planner"] == "rules" and len(_ids(out)) == 4
    assert any("parsed by rules" in w for w in out["warnings"])


def test_rules_window_wins_over_llm_arithmetic(db, team, monkeypatch):
    P.clear_cache()
    wrong = _llm_plan(time={**_llm_plan()["time"], "start": "2026-09-15T00:00:00+03:00"})
    monkeypatch.setattr("kb.reasoning.ask.get_llm", lambda budget=None: FakeLLM([wrong]))
    p = resolve_principal(db, team, "user:admin")
    out = ask(db, p, "Which meetings were held within the team over the past week?", now=NOW, mode="auto")
    assert out["plan"]["time"]["start"].startswith("2026-09-16") and out["plan"]["notes"]
