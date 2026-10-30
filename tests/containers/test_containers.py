import json
import subprocess
from pathlib import Path

import pytest

from variator.containers import RECIPES, ContainerError, ContainerSpec, DockerCLI, Mount, run_recipe

TRIVY = {
    "Results": [
        {
            "Vulnerabilities": [
                {"VulnerabilityID": "CVE-1", "PkgName": "lib", "Severity": "CRITICAL", "FixedVersion": "2.0"},
                {"VulnerabilityID": "CVE-2", "PkgName": "lib", "Severity": "LOW"},
            ],
            "Secrets": [{"RuleID": "aws"}],
        }
    ]
}
SCC = [
    {"Name": "Python", "Count": 10, "Code": 900, "Complexity": 80},
    {"Name": "YAML", "Count": 2, "Code": 40},
]


class FakeDocker:
    """Records docker commands; writes the given report into the /out mount."""

    def __init__(self, reports: dict[str, object] | None = None, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.reports = reports or {}
        self.returncode = returncode

    def __call__(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        out = next((a.rsplit(":", 1)[0] for a in args if a.endswith(":/out")), None)
        if "run" in args and out is not None:
            for name, body in self.reports.items():
                Path(out, name).write_text(json.dumps(body), encoding="utf-8")
        return subprocess.CompletedProcess(args, self.returncode, stdout="ok", stderr="")


def test_run_args_are_safe_by_default(tmp_path):
    spec = ContainerSpec(image="img:1", command=("tool", "--x"), mounts=(Mount(str(tmp_path), "/src"),))
    args = spec.run_args(tmp_path / "out")
    assert args[:2] == ["run", "--rm"]
    assert args[args.index("--network") + 1] == "none"
    assert "no-new-privileges" in args
    assert any(a.endswith(":/src:ro") for a in args)
    assert args[-3:] == ["img:1", "tool", "--x"]


def test_recipe_run_parses_compact_metrics(tmp_path):
    fake = FakeDocker({"trivy.json": TRIVY})
    outcome = run_recipe(DockerCLI(executor=fake), RECIPES["trivy"], tmp_path, out_dir=tmp_path / "o")
    assert outcome.ok
    assert outcome.metrics["vulnerabilities"] == {"CRITICAL": 1, "LOW": 1}
    assert outcome.metrics["secrets"] == 1 and outcome.metrics["top"][0]["id"] == "CVE-1"
    assert fake.calls[0][fake.calls[0].index("--network") + 1] == "bridge"


def test_missing_report_is_an_error_not_a_crash(tmp_path):
    outcome = run_recipe(
        DockerCLI(executor=FakeDocker(returncode=2)), RECIPES["scc"], tmp_path, out_dir=tmp_path / "o"
    )
    assert not outcome.ok and "no scc.json" in outcome.error


def test_deploy_redeploy_and_status():
    fake = FakeDocker()
    docker = DockerCLI(executor=fake)
    spec = ContainerSpec(image="svc:2", name="analyzer", ports={8080: 80})
    docker.redeploy(spec, pull=True)
    verbs = [c[1] for c in fake.calls]
    assert verbs == ["pull", "rm", "run"]
    run = fake.calls[-1]
    assert "-d" in run and "--rm" not in run and "127.0.0.1:8080:80" in run
    assert docker.status("analyzer") == "ok"
    with pytest.raises(ValueError):
        docker.deploy(ContainerSpec(image="x"))


def test_missing_docker_binary():
    def boom(args, timeout):
        raise FileNotFoundError("docker")

    with pytest.raises(ContainerError, match="not found"):
        DockerCLI(executor=boom).pull("x")
    assert DockerCLI(binary="definitely-not-docker").available() is False


def test_code_scan_tool(tmp_path):
    import asyncio

    from fastmcp import Client

    from variator.mcp import Services, build_server

    fake = FakeDocker({"scc.json": SCC})
    server = build_server(
        {},
        remotes={},
        services=Services({}, runner=DockerCLI(executor=fake), report_dir=tmp_path),
        all_tools=True,
    )

    async def go():
        async with Client(server) as c:
            ok = await c.call_tool("code_scan", {"source": str(tmp_path), "recipes": ["scc"]})
            bad = await c.call_tool(
                "code_scan", {"source": str(tmp_path), "recipes": ["rm_rf"]}, raise_on_error=False
            )
            return ok.data, bad

    data, bad = asyncio.run(go())
    assert (
        data["scans"]["scc"]["code_lines"] == 940
        and data["scans"]["scc"]["languages"]["Python"]["files"] == 10
    )
    assert bad.is_error and "unknown recipe or group" in bad.content[0].text


def test_nebius_job_runner_stages_and_collects(tmp_path):
    from types import SimpleNamespace

    from variator.cloud.specs import JobState
    from variator.containers import NebiusJobRunner

    src = tmp_path / "code"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (src / ".git").mkdir()
    staging = tmp_path / "bucket"

    class FakeJobs:
        def __init__(self):
            self.specs = []

        def create_job(self, spec, dry_run=False):
            self.specs.append(spec)
            run_dir = staging / spec.volumes[-1].source_path  # <run>/out
            assert (staging / spec.volumes[0].source_path / "pkg" / "a.py").is_file()
            assert not (staging / spec.volumes[0].source_path / ".git").exists()
            run_dir.joinpath("scc.json").write_text(json.dumps(SCC), encoding="utf-8")
            return SimpleNamespace(resource_id="job-1")

        def wait_for_job(self, job_id, **kwargs):
            return SimpleNamespace(state=JobState.COMPLETED)

    jobs = FakeJobs()
    runner = NebiusJobRunner(jobs, bucket="bkt-1", staging=staging)
    outcome = run_recipe(runner, RECIPES["scc"], src, out_dir=tmp_path / "o")
    assert outcome.ok and outcome.metrics["code_lines"] == 940
    spec = jobs.specs[0]
    assert spec.image == RECIPES["scc"].image and spec.args.startswith("scc --format json")
    assert [(v.container_path, v.mode) for v in spec.volumes] == [("/src", "ro"), ("/out", "rw")]
    assert spec.timeout_s == 3600 and spec.platform == "cpu-d3"


def test_runner_from_env():
    from variator.containers.nebius import runner_from_env

    assert isinstance(runner_from_env({}), DockerCLI)
    with pytest.raises(ValueError, match="VARIATOR_SCAN_BUCKET"):
        runner_from_env({"VARIATOR_RUNNER": "nebius"})
    with pytest.raises(ValueError, match="unknown"):
        runner_from_env({"VARIATOR_RUNNER": "k8s"})
