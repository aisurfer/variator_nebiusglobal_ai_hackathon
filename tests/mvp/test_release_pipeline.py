"""Precompute pipeline (scripts/mvp/pipeline.py): hashing, stage states, skipping, gating, adoption.

No Docker and no network: the stages here are small fakes that write files under a temporary root.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    name = "mvp_pipeline_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/mvp/pipeline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up in sys.modules
    spec.loader.exec_module(module)
    return module


P = _load()


def test_expand_braces() -> None:
    assert P.expand("src/{a,b}/**/*.py") == ["src/a/**/*.py", "src/b/**/*.py"]
    assert P.expand("x/{a,b}/{c,d}") == ["x/a/c", "x/a/d", "x/b/c", "x/b/d"]
    assert P.expand("plain/*.py") == ["plain/*.py"]


def test_digest_paths(tmp_path: Path) -> None:
    d = tmp_path / "set"
    (d / "sub").mkdir(parents=True)
    (d / "a.json").write_text("{}")
    (d / "sub/b.json").write_text("[]")
    first = P.digest_paths([d], tmp_path)
    assert first == P.digest_paths([d], tmp_path)
    (d / "sub/b.json").write_text("[1]")
    assert P.digest_paths([d], tmp_path) != first
    assert P.digest_paths([tmp_path / "missing"], tmp_path) is None
    (d / "__pycache__").mkdir()
    (d / "__pycache__/x.pyc").write_bytes(b"\0")
    assert P.digest_paths([d], tmp_path) == P.digest_paths([d], tmp_path)


def test_every_stage_has_an_owner_and_known_upstreams() -> None:
    names = [s.name for s in P.STAGES]
    assert len(names) == len(set(names))
    for i, s in enumerate(P.STAGES):
        assert s.owner.startswith("WP") and s.cost in ("cheap", "llm", "kb", "stack")
        for u in s.after:
            assert u in names[:i], f"{s.name} runs before its upstream {u}"
    assert names[:4] == ["history", "narrative", "intake", "kb-load"]
    assert names[-2:] == ["artifacts", "publish"]


def _fake_stages(calls: list[str]) -> list:
    def writer(name: str):
        def run(c) -> None:
            calls.append(name)
            out = c.artifacts / name
            out.mkdir(parents=True, exist_ok=True)
            (out / "manifest.json").write_text(json.dumps({"kind": name, "seed": c.seed}))

        return run

    return [
        P.Stage("a", "WP0", "first", writer("a"), outputs=lambda c: [c.artifacts / "a"],
                params=lambda c: {"seed": c.seed}),
        P.Stage("b", "WP0", "second", writer("b"), outputs=lambda c: [c.artifacts / "b"], after=("a",)),
        P.Stage("k", "WP0", "kb stage", writer("k"), outputs=lambda c: [c.artifacts / "k"], cost="kb"),
        P.Stage("gone", "WP0", "not landed", writer("gone"), available=lambda c: "not landed yet"),
    ]  # fmt: skip


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    stages = _fake_stages(calls)
    monkeypatch.setattr(P, "STAGES", stages)
    monkeypatch.setattr(P, "BY_NAME", {s.name: s for s in stages})
    return calls


def _run(root: Path, *extra: str) -> int:
    return P.main(["--root", str(root), "run", *extra])


def test_run_skips_fresh_stages_and_gates_kb(tmp_path: Path, fake: list[str], capsys) -> None:
    assert _run(tmp_path, "--dry-run") == 0
    assert fake == [] and not (tmp_path / "pipeline.json").exists()
    out = capsys.readouterr().out
    assert (
        "a              run" in out
        and "k              blocked     needs --kb" in out
        and "unavailable" in out
    )

    assert _run(tmp_path) == 2  # k is blocked without --kb
    assert fake == ["a", "b"]
    manifest = json.loads((tmp_path / "pipeline.json").read_text())
    assert manifest["stages"]["a"]["status"] == "done" and "k" not in manifest["stages"]
    assert (tmp_path / "logs/a.log").is_file()

    assert _run(tmp_path, "--kb") == 0
    assert fake == ["a", "b", "k"]  # a and b are fresh now
    assert _run(tmp_path, "--kb") == 0 and fake == ["a", "b", "k"]

    assert _run(tmp_path, "--kb", "--force", "b") == 0 and fake[-1] == "b"


def test_changed_inputs_or_outputs_rerun(tmp_path: Path, fake: list[str], capsys) -> None:
    assert _run(tmp_path, "--kb") == 0
    fake.clear()
    # a parameter of a changes: a runs, and b runs because a's output changed
    assert P.main(["--root", str(tmp_path), "--seed", "8", "run", "--kb"]) == 0
    assert fake == ["a", "b"]
    fake.clear()
    # an output edited on disk is not trusted
    (tmp_path / "artifacts/b/manifest.json").write_text("{}")
    capsys.readouterr()
    assert P.main(["--root", str(tmp_path), "--seed", "8", "run", "--kb", "--dry-run"]) == 0
    assert "outputs changed on disk" in capsys.readouterr().out
    assert P.main(["--root", str(tmp_path), "--seed", "8", "run", "--kb"]) == 0
    assert fake == ["b"]


def test_failed_stage_stops_its_downstream(tmp_path: Path, fake: list[str], monkeypatch) -> None:
    def boom(c) -> None:
        raise P.StageError("broken")

    monkeypatch.setattr(P.BY_NAME["a"], "run", boom)
    assert _run(tmp_path, "--kb") == 1
    manifest = json.loads((tmp_path / "pipeline.json").read_text())
    assert manifest["stages"]["a"]["status"] == "failed" and "broken" in manifest["stages"]["a"]["error"]
    assert "b" not in manifest["stages"]  # waiting for a
    assert manifest["last_run"]["stages"]["b"][0] == "waiting"


def test_check_set_rejects_bad_json(tmp_path: Path) -> None:
    s = tmp_path / "s"
    s.mkdir()
    (s / "manifest.json").write_text(json.dumps({"kind": "x", "asOf": "2026-10-26"}))
    (s / "ok.json").write_text("[]")
    summary = P.check_set(s)
    assert summary["files"] == 2 and summary["kind"] == "x" and summary["asOf"] == "2026-10-26"
    (s / "bad.json").write_text("{")
    with pytest.raises(P.StageError, match="not JSON"):
        P.check_set(s)


def test_cli_commands_of_a_missing_module() -> None:
    assert P.cli_commands("variator.no_such_module.cli") is None


def test_adopt_a_writers_work_directory(tmp_path: Path) -> None:
    work = tmp_path / "old-work"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"artifacts": [{"id": "x"}, {"id": "y"}]}))
    (work / "texts.json").write_text(json.dumps({"x": {"status": "accepted"}}))
    root = tmp_path / "root"
    assert P.main(["--root", str(root), "adopt", "narrative", "--from", str(work)]) == 0
    assert P.narrative_pending(root / "work/narrative") == ["y"]
    assert not (root / "pipeline.json").exists()  # the stage itself still runs (plan, check, apply)


def test_secrets_never_reach_the_stages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OTHER_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("OTHER_AUTH_TOKEN", "t-test")
    monkeypatch.setenv("NEBIUS_API_KEY", "k-test")
    env = P.Ctx(root=tmp_path).env()
    assert "OTHER_LLM_API_KEY" not in env and "OTHER_AUTH_TOKEN" not in env
    assert env["NEBIUS_API_KEY"] == "k-test" and env["PYTHONUNBUFFERED"] == "1"


def test_an_unchanged_result_keeps_the_directory(tmp_path: Path) -> None:
    final = tmp_path / "history"
    (final / "datasets").mkdir(parents=True)
    (final / "datasets/a.csv").write_text("1\n")
    inode = final.stat().st_ino  # a bind mount (variator-api) holds this directory
    tmp = P.fresh_tmp(final)
    (tmp / "datasets").mkdir(parents=True)
    (tmp / "datasets/a.csv").write_text("1\n")
    assert P.replace_dir(tmp, final) is False
    assert final.stat().st_ino == inode and not tmp.exists()
    tmp = P.fresh_tmp(final)
    (tmp / "datasets").mkdir(parents=True)
    (tmp / "datasets/a.csv").write_text("2\n")
    assert P.replace_dir(tmp, final) is True
    assert (final / "datasets/a.csv").read_text() == "2\n" and not tmp.exists()


def test_branch_deliveries_follow_the_report(tmp_path: Path) -> None:
    c = P.Ctx(root=tmp_path)
    for t in ("company-b-a-p50", "company-b-old-p50"):
        f = c.work / "branches/branches" / t / "delivery" / P.SEED_FILES[0]
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")
    assert sorted(P.branch_deliveries(c)) == ["company-b-a-p50", "company-b-old-p50"]
    report = {"branches": [{"tenant": "company-b-a-p50"}]}
    (c.work / "branches/report.json").write_text(json.dumps(report))
    assert list(P.branch_deliveries(c)) == ["company-b-a-p50"]  # an older run's directory stays on disk
    assert P.branch_tenants(c) == ["company-b-a-p50"]


def _fake_branch_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> P.Ctx:
    """A branch_day.py that writes its day data and records its arguments; it never calls a writer."""
    repo = tmp_path / "repo"
    (repo / "scripts/mvp").mkdir(parents=True)
    (repo / "scripts/mvp/branch_day.py").write_text(
        "import json, sys, pathlib\n"
        "a = sys.argv\n"
        "out, day = pathlib.Path(a[a.index('--out') + 1]), pathlib.Path(a[a.index('--day-out') + 1])\n"
        "(out / 'texts').mkdir(parents=True, exist_ok=True)\n"
        "(out / 'argv.json').write_text(json.dumps(a[1:]))\n"
        "day.mkdir(parents=True)\n"
        "(day / 'day_examples.json').write_text('{}')\n"
    )
    monkeypatch.setattr(P, "REPO", repo)
    c = P.Ctx(root=tmp_path / "root")
    c.log = tmp_path / "stage.log"
    (c.work / "branches/texts").mkdir(parents=True)
    (c.work / "branches/texts/texts.json").write_text("before")
    return c


def test_branches_without_llm_use_accepted_texts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _fake_branch_day(tmp_path, monkeypatch)
    P.run_branches(c)
    assert (c.work / "day/day_examples.json").is_file()
    assert not (c.work / "no-llm").exists() and not (c.work / "branches.before").exists()


def test_branches_without_llm_write_placeholder_texts_never_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # since the Nemotron migration, a run without --llm asks branch_day.py for deterministic placeholder texts
    # (no writer call) instead of refusing the writers' calls; with --llm the writers write the texts
    c = _fake_branch_day(tmp_path, monkeypatch)
    argv = lambda: json.loads((c.work / "branches/argv.json").read_text())  # noqa: E731
    P.run_branches(c)
    assert argv()[argv().index("--texts") + 1] == "placeholder"
    assert "branches: deterministic placeholder text (no writer call)" in c.notes
    assert (c.work / "day/day_examples.json").is_file()
    c.llm, c.notes = True, []
    P.run_branches(c)
    assert "--texts" not in argv() and c.notes == []
