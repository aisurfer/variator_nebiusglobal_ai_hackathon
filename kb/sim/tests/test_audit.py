"""Audit of a finished world (kbsim.audit) over the mini world: order of replies and leaks of future facts."""
from __future__ import annotations

from datetime import timedelta

from kbsim.audit import Audit
from kbsim.models import Fact
from kbsim.tenant import TenantState
from tests.mini import at, build_mini


def test_a_clean_world_has_no_order_findings(tmp_path):
    build_mini(tmp_path)
    found = Audit(TenantState("mini", tmp_path)).run()
    assert "order" not in found and "absent" not in found and "future" not in found


def test_audit_finds_order_and_future_leaks(tmp_path):
    build_mini(tmp_path)
    s = TenantState("mini", tmp_path)
    pay05 = next(b for b in s.scenario.beats if b.id == "PAY-05")
    pay05.facts.append(Fact(key="sum", any=["1,140,000"]))  # introduced on day 3
    t0 = at(1, "09:00")
    first = s.w.send_email(ts=t0 + timedelta(hours=1), day=1, sender="ceo", to=["accountant"], cc=[],
                           subject="Reserve", body="I counted: up to 1,140,000 will go to the reserve.")
    s.w.send_email(ts=t0, day=1, sender="accountant", to=["ceo"], cc=[], subject="Re: Reserve", body="OK",
                   reply_to=first)
    found = Audit(s).run()
    assert [x["artifact"] for x in found["future"]] == [first]
    assert any("reply sent before its parent" in x["msg"] for x in found["order"])


def test_absence_and_banned_terms(tmp_path):
    build_mini(tmp_path)
    tdir = tmp_path / "tenants" / "mini"
    (tdir / "availability.yaml").write_text("absences:\n  accountant: [[2026-06-16, 2026-06-16]]\n", encoding="utf-8")
    (tdir / "banned_terms.yaml").write_text("banned: ['kassa ?plus']\n", encoding="utf-8")  # case-insensitive regexes
    found = Audit(TenantState("mini", tmp_path)).run()
    assert [x["actor"] for x in found["absent"]] == ["accountant"]  # the comment of day 2
    assert {x["kind"] for x in found["banned"]} == {"page.created"}
