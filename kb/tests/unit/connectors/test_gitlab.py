"""GitLab adapter: merge requests, commits, releases, issues; closing patterns and release contents."""
from __future__ import annotations

from kb.connectors.base import get_adapter
from tests.unit.connectors.conftest import causes, ext, make_env, normalize, sources, statements

GL = "gitlab-vf"


def _mr(description: str, title: str = "t", branch: str = "b") -> dict:
    return {"iid": 1, "title": title, "description": description, "state": "opened", "source_branch": branch}


def test_text_patterns():
    (r,) = normalize(make_env(GL, "merge_request", "flowpilot/app!1", _mr(
        "Fixes FP-120, FP-121 and #3.\nImplements EXP-9\nResolves group/other#5\n"
        "Relates to FP-101. See ATL-58. Unknown ABC-1, UTF-8.\nCloses https://vectorforge.atlassian.net/browse/FP-7"),
        container="flowpilot/app"))
    assert causes(r) == {
        ("closes", ext("jira-vf", "issue", "FP-120")), ("closes", ext("jira-vf", "issue", "FP-121")),
        ("closes", ext(GL, "issue", "flowpilot/app#3")), ("closes", ext("jira-vf", "issue", "EXP-9")),
        ("closes", ext(GL, "issue", "group/other#5")), ("closes", ext("jira-vf", "issue", "FP-7")),
    }
    assert statements(r, "relates_to") == [ext("jira-vf", "issue", "FP-101"), ext("jira-vf", "issue", "ATL-58")]
    assert statements(r, "mentions") == []  # ABC is not a Jira key of the integration, UTF-8 is not a key


def test_ref_from_url():
    a, src = get_adapter("gitlab"), sources().sources[GL]
    base = "https://gitlab.vectorforge.io/flowpilot/app/-"
    assert a.ref_from_url(f"{base}/merge_requests/45", src).external_key() == ext(GL, "merge_request", "flowpilot/app!45")
    assert a.ref_from_url(f"{base}/merge_requests/45/diffs", src).external_id == "flowpilot/app!45"
    assert a.ref_from_url(f"{base}/releases/v2.4.0", src).external_key() == ext(GL, "release", "flowpilot/app@v2.4.0")
    assert a.ref_from_url(f"{base}/tags/v2.5.0", src).external_id == "flowpilot/app@v2.5.0"
    assert a.ref_from_url(f"{base}/issues/77", src).external_key() == ext(GL, "issue", "flowpilot/app#77")
    assert a.ref_from_url(f"{base}/commit/{'a' * 40}", src).external_id == f"flowpilot/app@{'a' * 40}"
    assert a.ref_from_url("https://gitlab.vectorforge.io/flowpilot/app", src) is None
    assert a.ref_from_url("https://gitlab.example.com/flowpilot/app/-/issues/1", src) is None


def test_delete_and_degenerate_payloads():
    for ot, eid in (("merge_request", "flowpilot/app!9"), ("issue", "flowpilot/app#9"),
                    ("release", "flowpilot/app@v9"), ("commit", "flowpilot/app@" + "b" * 40)):
        (gone,) = normalize(make_env(GL, ot, eid, None, container="flowpilot/app", op="delete"))
        assert gone.deleted and gone.entity_id.startswith("gitlab.flowpilot-app.")
        (r,) = normalize(make_env(GL, ot, eid, {}, container="flowpilot/app"))
        assert r.entity_id == gone.entity_id
