import io
import json
import os
import subprocess
import sys

import pytest

from variator.cloud import ExitCode, InputTooLarge, JobInput, JobOutput, job_entry, parse_marker
from variator.cloud.job_io import (
    INPUT_B64_ENV,
    INPUT_PATH_ENV,
    MARKER_PREFIX,
    MAX_ENV_INPUT_CHARS,
    OUTPUT_PATH_ENV,
    format_marker,
)


def run_main(argv=(), environ=None):
    out, err = io.StringIO(), io.StringIO()
    code = job_entry.main(list(argv), environ=environ or {}, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def marker_of(stdout: str):
    last = stdout.strip().splitlines()[-1]
    assert last.startswith(MARKER_PREFIX)
    return parse_marker(last)


def test_file_input_writes_output_and_marker(tmp_path, decision, engine):
    job = JobInput(run_id="run-1", decision=decision, n_scenarios=2_000_000, seed=7, backend="cupy")
    in_path = tmp_path / "in.json"
    in_path.write_bytes(job.to_json_bytes())
    out_path = tmp_path / "bucket" / "variator-runs" / "run-1" / "output.json"

    code, stdout, stderr = run_main(["--input", str(in_path), "--output", str(out_path)])

    assert code == ExitCode.OK
    model, kwargs = engine.simulate_calls[0]
    assert model == decision
    assert kwargs == {"n_scenarios": 2_000_000, "seed": 7, "backend": "cupy", "sensitivity": True}
    assert not engine.optimize_calls
    output = JobOutput.model_validate_json(out_path.read_bytes())
    assert output.ok and output.exit_code == 0 and output.run_id == "run-1"
    assert output.backend == "cupy" and output.simulation.n_scenarios == 1_000_000
    assert output.optimization is None and output.notes == ["correlation matrix repaired"]
    assert output.timings.simulate_s is not None and output.timings.total_s >= output.timings.simulate_s
    marker = marker_of(stdout)
    assert marker.ok and marker.run_id == "run-1" and marker.output_path == str(out_path)
    # small enough to be inlined; only write_s differs (measured after the file was written)
    assert marker.output.model_dump(exclude={"timings"}) == output.model_dump(exclude={"timings"})
    assert marker.output.timings.write_s is not None
    assert "simulating 2,000,000 scenarios" in stderr


def test_env_input_and_env_output_path(tmp_path, decision, engine):
    job = JobInput(decision=decision, n_scenarios=1000)
    out_path = tmp_path / "out.json"
    environ = {INPUT_B64_ENV: job.to_env_value(), OUTPUT_PATH_ENV: str(out_path)}
    code, stdout, _ = run_main(environ=environ)
    assert code == 0
    assert JobOutput.model_validate_json(out_path.read_bytes()).run_id == job.run_id
    assert engine.simulate_calls[0][1]["n_scenarios"] == 1000


def test_input_path_takes_precedence_over_env(tmp_path, decision, engine):
    from_file = JobInput(run_id="from-file", decision=decision)
    from_env = JobInput(run_id="from-env", decision=decision)
    path = tmp_path / "in.json"
    path.write_bytes(from_file.to_json_bytes())
    _, stdout, _ = run_main(environ={INPUT_PATH_ENV: str(path), INPUT_B64_ENV: from_env.to_env_value()})
    assert marker_of(stdout).run_id == "from-file"


def test_simulate_and_optimize(tmp_path, decision, engine, opt_result):
    job = JobInput(
        decision=decision, task="simulate_and_optimize", budget=3e6, max_cvar_loss=-1e5, solver="cuopt"
    )
    code, stdout, _ = run_main(environ={INPUT_B64_ENV: job.to_env_value()})
    assert code == 0
    run, model, kwargs = engine.optimize_calls[0]
    assert model == decision and run.option_names == ["second_warehouse", "expand_current"]
    assert kwargs == {"budget": 3e6, "max_cvar_loss": -1e5, "solver": "cuopt"}
    marker = marker_of(stdout)
    assert marker.output_path is None  # no output path: the marker is the only copy
    assert marker.output.optimization == opt_result


def test_optimization_failure_keeps_simulation(tmp_path, decision, engine):
    engine.optimize_error = RuntimeError("cuOpt needs a GPU")
    out_path = tmp_path / "out.json"
    job = JobInput(decision=decision, task="simulate_and_optimize")
    code, stdout, _ = run_main(["--output", str(out_path)], environ={INPUT_B64_ENV: job.to_env_value()})
    assert code == ExitCode.OPTIMIZATION_FAILED
    output = JobOutput.model_validate_json(out_path.read_bytes())
    assert not output.ok and output.simulation is not None and output.optimization is None
    assert output.errors[0].stage == "optimize" and "GPU" in output.errors[0].message
    assert marker_of(stdout).errors == ["optimize: RuntimeError: cuOpt needs a GPU"]


def test_simulation_failure(decision, engine):
    engine.simulate_error = ValueError("NaN metric")
    code, stdout, _ = run_main(environ={INPUT_B64_ENV: JobInput(decision=decision).to_env_value()})
    assert code == ExitCode.SIMULATION_FAILED
    marker = marker_of(stdout)
    assert not marker.ok and marker.exit_code == 4
    assert marker.output.simulation is None and marker.output.errors[0].type == "ValueError"


def test_no_input_is_a_usage_error(engine):
    code, stdout, stderr = run_main()
    assert code == ExitCode.USAGE
    assert marker_of(stdout).errors[0].startswith("input:")
    assert "no input" in stderr
    assert not engine.simulate_calls


@pytest.mark.parametrize(
    "environ",
    [
        {INPUT_B64_ENV: "not base64 !!!"},
        {INPUT_B64_ENV: "eyJub3QiOiAiYSBqb2IifQ=="},  # {"not": "a job"}
        {INPUT_PATH_ENV: "/nonexistent/input.json"},
    ],
)
def test_invalid_input(environ, engine):
    code, stdout, _ = run_main(environ=environ)
    assert code == ExitCode.INVALID_INPUT
    assert not marker_of(stdout).ok
    assert not engine.simulate_calls


def test_env_input_size_limit(decision, engine):
    code, stdout, _ = run_main(environ={INPUT_B64_ENV: "A" * (MAX_ENV_INPUT_CHARS + 4)})
    assert code == ExitCode.INVALID_INPUT
    assert "limit" in marker_of(stdout).errors[0]


def test_host_side_env_limit(decision):
    job = JobInput(decision=decision)
    with pytest.raises(InputTooLarge, match="bucket"):
        job.to_env_value(limit=100)


def test_output_write_failure(tmp_path, decision, engine):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code, stdout, _ = run_main(
        ["--output", str(blocker / "out.json")],
        environ={INPUT_B64_ENV: JobInput(decision=decision).to_env_value()},
    )
    assert code == ExitCode.OUTPUT_FAILED
    marker = marker_of(stdout)
    assert marker.output_path is None and marker.output.simulation is not None
    assert marker.errors[-1].startswith("output:")


def test_marker_inlines_only_small_outputs(decision, engine):
    output = job_entry.execute(JobInput(decision=decision))
    big = parse_marker(format_marker(output, "/data/x.json", inline_limit=10))
    assert big.output is None and big.output_path == "/data/x.json" and big.ok
    small = parse_marker(format_marker(output, None))
    assert small.output.run_id == output.run_id


def test_parse_marker_ignores_noise_and_takes_last():
    first = MARKER_PREFIX + json.dumps({"run_id": "a", "ok": False, "exit_code": 4})
    last = MARKER_PREFIX + json.dumps({"run_id": "b", "ok": True, "exit_code": 0})
    logs = "\n".join(["boot", f"2026-10-25T10:00:00Z {first}", f"2026-10-25T10:01:00Z {last}", "", "bye"])
    assert parse_marker(logs).run_id == "b"
    assert parse_marker(f"{MARKER_PREFIX}{{broken") is None
    assert parse_marker("nothing here") is None


def test_module_entrypoint_runs_as_a_process():
    env = {k: v for k, v in os.environ.items() if not k.startswith("VARIATOR_JOB_")}
    proc = subprocess.run(
        [sys.executable, "-m", "variator.cloud.job_entry"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == ExitCode.USAGE
    assert proc.stdout.strip().startswith(MARKER_PREFIX)
    assert "RuntimeWarning" not in proc.stderr
