"""An English tenant: causal explanations over REST and /ask answers come in English with citations."""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.api import routes_causal
from kb.db import Jsonb
from kb.reasoning.ask import ask
from tests.integration.causal import story as S

pytestmark = pytest.mark.integration
NOW = datetime.fromisoformat("2026-10-24T12:00:00+03:00")
_CYR = re.compile("[\u0400-\u04ff]")


def _no_russian(text: str) -> list[str]:
    """Lines with Cyrillic letters outside quoted titles and quotes (a guard: this copy answers in English only)."""
    bare = re.sub(r"“[^”]*”|«[^»]*»", "", text)
    return [line for line in bare.splitlines() if _CYR.search(line)]


@pytest.fixture
def english(db, story, tenant):
    db.execute("UPDATE tenant SET settings = settings || %s WHERE tenant_id = %s", (Jsonb({"language": "en"}), tenant))
    return story


@pytest.fixture
def client(db, tenant):
    app = FastAPI()
    app.include_router(routes_causal.router)
    with TestClient(app, headers={"X-KB-Tenant": tenant, "X-KB-Principal": "user:admin"}) as c:
        yield c


def test_rest_why_explains_in_english(english, client):
    r = client.get(f"/api/v1/causal/why/{S.EXP}", params={"depth": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["summary_lines"][0].startswith("experiment “EXP-7")
    assert " exists because:" in body["summary_lines"][0]
    assert any("the link is declared in the source, Jira (declared:jira_import)" in s for s in body["summary_lines"])
    assert any(s.startswith("Root causes: ") for s in body["summary_lines"])
    assert body["interpretation"].startswith("“Why” means the grounds")
    assert not _no_russian(body["summary"]), _no_russian(body["summary"])
    gov = next(x for x in body["links"] if x["predicate"] == "governed_by")
    assert gov["predicate_label"] == "governed by"


def test_rest_stale_and_impact_in_english(english, client):
    stale = client.get("/api/v1/causal/stale").json()
    assert stale["items"] and all(" relies on " in it["explanation"] for it in stale["items"])
    imp = client.post("/api/v1/causal/impact", json={"entity_id": S.POLICY, "fields": ["rules"]}).json()
    assert imp["explanation_lines"][0].startswith("A change of fields rules of policy “Experiment policy”")


def test_ask_in_english_answers_in_english_with_citations(db, english, admin):
    out = ask(db, admin, "Why did we launch experiment EXP-7?", mode="deterministic", now=NOW)
    assert out["language"] == "en" and out["intent"] == "why" and out["subject"]["id"] == S.EXP
    assert out["answer"].startswith("Subject: “") and "Growth sync" in out["answer"]
    assert "In short: " in out["answer"] and "Root causes:" in out["answer"]
    assert any(c.startswith(S.MEETING) for c in out["citations"])
    assert not _no_russian(out["answer"]), _no_russian(out["answer"])


def test_ask_not_found_in_english(db, english, admin):
    out = ask(db, admin, "What do we know about the Zephyr acquisition?", mode="deterministic", now=NOW)
    assert out["language"] == "en"
    assert not _no_russian("\n".join(out["unknowns"] + [out["answer"]]))
