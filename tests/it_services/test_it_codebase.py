import subprocess
from pathlib import Path

import pytest
from variator_it_services.codebase import (
    architecture_report,
    component_metrics,
    git_churn,
    import_graph,
    package_graph,
    strongly_connected,
)

FILES = {
    "src/app/__init__.py": "",
    "src/app/core/__init__.py": "",
    "src/app/core/model.py": "def f(x):\n    if x > 1:\n        return 1\n    return 0\n",
    "src/app/api/__init__.py": "",
    "src/app/api/routes.py": (
        "from app.core import model\nfrom ..db import store\n\ndef g():\n    return model.f(2)\n"
    ),
    "src/app/db/__init__.py": "",
    "src/app/db/store.py": "from app.api.routes import g\n",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    (tmp_path / "src/app/core/model.py").write_text(
        FILES["src/app/core/model.py"] + "\n# change\n", encoding="utf-8"
    )
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two")
    return tmp_path


def test_churn_counts_commits(repo):
    churn = git_churn(repo)
    assert churn["src/app/core/model.py"]["commits"] == 2
    assert churn["src/app/db/store.py"]["commits"] == 1
    assert git_churn(repo / "src") != {}  # inside the repo also works
    assert git_churn(Path("/definitely/not/a/repo")) == {}


def test_import_graph_resolves_absolute_and_relative(repo):
    files = [f for f in FILES]
    graph = import_graph(repo, files)
    assert graph["app.api.routes"] == {"app.core.model", "app.db.store"}
    assert graph["app.db.store"] == {"app.api.routes"}


def test_cycles_between_packages(repo):
    packages = package_graph(import_graph(repo, list(FILES)), depth=2)
    assert strongly_connected(packages) == [["app.api", "app.db"]]
    assert strongly_connected({"a": {"b"}, "b": set()}) == []


def test_metrics_are_cached_per_commit(repo, tmp_path_factory, monkeypatch):
    import variator_it_services.codebase as cb

    monkeypatch.setenv(cb.ENV_CACHE, str(tmp_path_factory.mktemp("cache")))
    calls = []
    real = cb._architecture
    monkeypatch.setattr(cb, "_architecture", lambda *a: calls.append(1) or real(*a))
    first = architecture_report(repo)
    assert architecture_report(repo) == first and len(calls) == 1  # second scan: from the cache
    (repo / "src/app/new.py").write_text("x = 1\n", encoding="utf-8")
    architecture_report(repo)
    assert len(calls) == 2  # uncommitted changes: no cache
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "three")
    assert architecture_report(repo)["files"] == first["files"] + 1 and len(calls) == 3  # new commit
    monkeypatch.setenv(cb.ENV_CACHE, "off")
    architecture_report(repo)
    assert len(calls) == 4


def test_component_and_architecture_metrics(repo):
    comps = component_metrics(
        repo, {"core": ["src/app/core/"], "api": ["src/app/api/"], "db": ["src/app/db/"]}
    )
    assert comps["core"]["commits"] == 3 and comps["core"]["ccn_max"] == 2
    assert comps["api"]["fan_out"] == 2 and comps["core"]["fan_in"] == 1
    assert comps["core"]["instability"] == 0.0
    report = architecture_report(repo)
    assert report["cycles"] == [["app.api", "app.db"]]
    assert report["hot_spots"][0]["file"] == "src/app/core/model.py"
