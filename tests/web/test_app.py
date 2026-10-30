"""variator.web: health, version, artifact listing and serving, path traversal, JSON errors.

Every test uses a tmp directory; no database, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from variator.web.app import create_app
from variator.web.settings import WebSettings
from variator.web.version import package_version, read_git_commit

SHA = "0123456789abcdef0123456789abcdef01234567"
SECRET = "top-secret-value"
# The canonical API path: caddy and the workspace proxy (workspace/server/variatorProxy.ts) keep it.
API = "/api/variator"


def _write(path: Path, data: object | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """tmp/secret.json (outside), tmp/artifacts/{demo, broken, notaset}."""
    _write(tmp_path / "secret.json", {"secret": SECRET})
    root = tmp_path / "artifacts"
    _write(root / "demo" / "manifest.json", {"title": "Demo set", "files": ["month/cube.json"]})
    _write(root / "demo" / "month" / "cube.json", {"metric": "revenue", "values": [1, 2, 3]})
    _write(root / "demo" / "notes.txt", "plain text")
    _write(root / "demo" / ".hidden.json", {"hidden": True})
    (root / "demo" / "link.json").symlink_to(tmp_path / "secret.json")
    (root / "demo" / "outside").symlink_to(tmp_path, target_is_directory=True)
    _write(root / "broken" / "manifest.json", "{not json")
    _write(root / "notaset" / "data.json", {"a": 1})
    return tmp_path


@pytest.fixture
def client(tree: Path) -> TestClient:
    settings = WebSettings(artifacts_dir=tree / "artifacts", git_commit=SHA, repo_root=None)
    return TestClient(create_app(settings))


def _assert_json_error(resp, status: int) -> None:
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["error"]["status"] == status
    assert isinstance(body["error"]["message"], str)
    assert SECRET not in resp.text


def test_health(client: TestClient) -> None:
    for url in ("/health", f"{API}/health"):  # /health without the prefix: container health checks
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "path",
    [
        "/version",
        "/artifacts",
        "/artifacts/demo/manifest.json",
        "/artifacts/demo/month/cube.json",
        "/nothing",
    ],
)
def test_only_health_works_without_the_prefix(client: TestClient, path: str) -> None:
    _assert_json_error(client.get(path), 404)
    assert client.get(API + path).status_code == (404 if path == "/nothing" else 200)


def test_unprefixed_health_is_get_only(client: TestClient) -> None:
    _assert_json_error(client.post("/health"), 405)


def test_version(client: TestClient) -> None:
    resp = client.get(f"{API}/version")
    assert resp.status_code == 200
    assert resp.json() == {"name": "variator", "version": package_version(), "git_commit": SHA}


def test_version_ignores_invalid_commit(tree: Path) -> None:
    settings = WebSettings(artifacts_dir=tree, git_commit="$(rm -rf /)", repo_root=None)
    resp = TestClient(create_app(settings)).get(f"{API}/version")
    assert resp.json()["git_commit"] is None


def test_settings_from_env() -> None:
    s = WebSettings.from_env({"VARIATOR_ARTIFACTS_DIR": "/data/artifacts", "VARIATOR_GIT_COMMIT": SHA})
    assert s.artifacts_dir == Path("/data/artifacts")
    assert s.git_commit == SHA
    default = WebSettings.from_env({})
    assert default.artifacts_dir == Path("artifacts")
    assert default.git_commit is None


def test_read_git_commit_branch_packed_detached(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    _write(git / "HEAD", "ref: refs/heads/main\n")
    _write(git / "refs" / "heads" / "main", SHA + "\n")
    assert read_git_commit(tmp_path) == SHA

    (git / "refs" / "heads" / "main").unlink()
    other = "f" * 40
    _write(git / "packed-refs", f"# pack-refs with: peeled\n{other} refs/heads/main\n")
    assert read_git_commit(tmp_path) == other

    _write(git / "HEAD", SHA)
    assert read_git_commit(tmp_path) == SHA


def test_read_git_commit_never_raises(tmp_path: Path) -> None:
    assert read_git_commit(None) is None
    assert read_git_commit(tmp_path) is None  # no .git
    _write(tmp_path / ".git" / "HEAD", "ref: refs/../../etc/passwd")
    assert read_git_commit(tmp_path) is None
    _write(tmp_path / ".git" / "HEAD", "garbage")
    assert read_git_commit(tmp_path) is None


def test_list_artifacts(client: TestClient) -> None:
    resp = client.get(f"{API}/artifacts")
    assert resp.status_code == 200
    sets = resp.json()["sets"]
    assert [s["id"] for s in sets] == ["broken", "demo"]  # notaset has no manifest.json
    assert sets[0] == {"id": "broken", "error": "manifest.json is not valid JSON"}
    assert sets[1] == {"id": "demo", "manifest": {"title": "Demo set", "files": ["month/cube.json"]}}


def test_list_artifacts_missing_dir(tmp_path: Path) -> None:
    settings = WebSettings(artifacts_dir=tmp_path / "absent", repo_root=None)
    resp = TestClient(create_app(settings)).get(f"{API}/artifacts")
    assert resp.status_code == 200
    assert resp.json() == {"sets": []}


def test_get_artifact_file(client: TestClient) -> None:
    resp = client.get(f"{API}/artifacts/demo/month/cube.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"metric": "revenue", "values": [1, 2, 3]}
    manifest = client.get(f"{API}/artifacts/demo/manifest.json")
    assert manifest.json()["title"] == "Demo set"


@pytest.mark.parametrize(
    "path",
    [
        "/artifacts/demo/%2e%2e/%2e%2e/secret.json",
        "/artifacts/demo/..%2F..%2Fsecret.json",
        "/artifacts/demo/month/%2e%2e/%2e%2e/%2e%2e/secret.json",
        "/artifacts/demo/%2Ftmp%2Fsecret.json",
        "/artifacts/demo/month%5C..%5C..%5Csecret.json",
        "/artifacts/%2e%2e/secret.json",
        "/artifacts/demo/link.json",  # symlink to a file outside the set
        "/artifacts/demo/outside/secret.json",  # symlinked directory outside the set
        "/artifacts/demo/.hidden.json",
        "/artifacts/demo/notes.txt",
    ],
)
def test_path_traversal_rejected(client: TestClient, path: str) -> None:
    _assert_json_error(client.get(API + path), 400)


def test_traversal_reaches_the_app_unnormalized(client: TestClient) -> None:
    """Guard of the test above: the client must not resolve the dot segments itself."""
    resp = client.get(f"{API}/artifacts/demo/%2e%2e/%2e%2e/secret.json")
    assert resp.json()["error"]["message"] == "invalid artifact path"


def test_missing_set_404(client: TestClient) -> None:
    _assert_json_error(client.get(f"{API}/artifacts/nope/manifest.json"), 404)
    _assert_json_error(client.get(f"{API}/artifacts/notaset/data.json"), 404)


def test_missing_file_404(client: TestClient) -> None:
    _assert_json_error(client.get(f"{API}/artifacts/demo/month/absent.json"), 404)
    _assert_json_error(client.get(f"{API}/artifacts/demo/manifest.json/x.json"), 404)


def test_unknown_route_and_method_are_json(client: TestClient) -> None:
    _assert_json_error(client.get(f"{API}/nothing"), 404)
    resp = client.post(f"{API}/artifacts")
    _assert_json_error(resp, 405)
    assert "GET" in resp.headers["allow"]


def test_unhandled_error_is_json(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(WebSettings(artifacts_dir=tree / "artifacts", repo_root=None))

    def boom(self):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(type(app.state.artifacts), "list_sets", boom)
    resp = TestClient(app, raise_server_exceptions=False).get(f"{API}/artifacts")
    _assert_json_error(resp, 500)
    assert "boom" not in resp.text
