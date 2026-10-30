"""The typed `causes` tool and the admin reindex endpoint."""
from __future__ import annotations

import pytest

from kb.acl import resolve_principal
from kb.contracts import CanonicalRecord, CausalSpec, Ref
from kb.journal.writer import journal_tx
from kb.reasoning.tools import ToolContext, call
from tests.conftest import at

pytestmark = pytest.mark.integration


def _rec(ext: str, acl: list[str], causes: list[CausalSpec] | None = None) -> CanonicalRecord:
    return CanonicalRecord(key=Ref(source_id="jira-vf", object_type="issue", external_id=ext), entity_id=f"jira.{ext}",
                           kind="Story", title=ext, fields={"summary": ext}, acl=acl, causes=causes or [])


@pytest.fixture
def data(db, tenant):
    with journal_tx(db, tenant, "ingest", at=at("2026-03-01T10:00:00+03:00")) as j:
        j.apply_records([_rec("FP-1", ["grp:flowpilot"])], origin="declared:jira_import")
        j.apply_records([_rec("FP-2", ["grp:flowpilot"], [CausalSpec(predicate="part_of", cause=Ref(
            source_id="jira-vf", object_type="issue", external_id="FP-1"))])], origin="declared:jira_import")
        j.apply_records([_rec("FP-3", ["grp:exec"])], origin="declared:jira_import")
    return tenant


def test_causes(db, data):
    admin = ToolContext(db, resolve_principal(db, data, "user:admin"))
    res = call(admin, "causes", {"version_id": "jira.FP-2@1"})
    assert [l["cause_version"] for l in res["links"]] == ["jira.FP-1@1"]


def test_admin_reindex(db, data):
    from fastapi.testclient import TestClient

    from kb.api.main import app

    with TestClient(app) as tc:
        r = tc.post("/api/v1/admin/reindex", params={"tenant": data}, json={"consumers": ["graph"]})
        assert r.status_code == 200 and r.json()["watermarks"]["graph"] == r.json()["watermarks"]["journal"]
        r = tc.post("/api/v1/admin/reindex", params={"tenant": data}, json={"consumers": ["graph"]},
                    headers={"X-KB-Principal": "user:jordan.baird"})
        assert r.status_code == 403
    from kb.worker.framework import reset

    reset(db, data, ["graph"])
