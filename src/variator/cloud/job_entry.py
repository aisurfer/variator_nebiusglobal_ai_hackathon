"""Container entrypoint: ``python -m variator.cloud.job_entry [--input PATH] [--output PATH]``.

Input, first match wins: ``--input`` path, ``$VARIATOR_JOB_INPUT_PATH``, base64 JSON in
``$VARIATOR_JOB_INPUT_B64``. Output: ``--output`` path or ``$VARIATOR_JOB_OUTPUT_PATH``
(normally inside a mounted bucket, because the container disk is wiped when the job
ends). The last stdout line is always ``VARIATOR_JOB_RESULT {json}``; progress goes to
stderr. Exit codes are listed in ``variator.cloud.job_io.ExitCode``.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from variator.cloud.errors import InputTooLarge
from variator.cloud.job_io import (
    INPUT_B64_ENV,
    INPUT_PATH_ENV,
    OUTPUT_PATH_ENV,
    ExitCode,
    JobError,
    JobInput,
    JobOutput,
    JobTimings,
    Stage,
    format_marker,
)


class _InputError(Exception):
    def __init__(self, message: str, code: ExitCode = ExitCode.INVALID_INPUT):
        self.code = code
        super().__init__(message)


def _simulate_fn() -> Callable[..., Any]:
    # Imported lazily: the simulation (and CuPy) is only needed inside the container.
    return importlib.import_module("variator.simulation").simulate


def _optimize_fn() -> Callable[..., Any]:
    return importlib.import_module("variator.optimization").optimize_decision


def load_input(input_path: str | None, environ: Mapping[str, str]) -> JobInput:
    """Read the job input from a file path or the base64 env var."""
    path = input_path or environ.get(INPUT_PATH_ENV)
    try:
        if path:
            return JobInput.from_json_bytes(Path(path).read_bytes())
        encoded = environ.get(INPUT_B64_ENV)
        if encoded:
            return JobInput.from_env_value(encoded)
    except FileNotFoundError as exc:
        raise _InputError(f"input file not found: {path}") from exc
    except InputTooLarge as exc:
        raise _InputError(str(exc)) from exc
    except ValidationError as exc:
        raise _InputError(f"invalid job input: {exc.error_count()} error(s): {exc.errors()[:3]}") from exc
    except (OSError, ValueError) as exc:
        raise _InputError(f"cannot read job input: {exc}") from exc
    raise _InputError(
        f"no input: pass --input, or set {INPUT_PATH_ENV} or {INPUT_B64_ENV}", code=ExitCode.USAGE
    )


def _error(stage: Stage, exc: BaseException) -> JobError:
    return JobError(stage=stage, type=type(exc).__name__, message=str(exc)[:2000])


def execute(job: JobInput, *, log: Callable[[str], None] = lambda _: None) -> JobOutput:
    """Run the simulation (and optimization) in-process and describe the outcome."""
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    output = JobOutput(
        run_id=job.run_id, task=job.task, ok=False, exit_code=ExitCode.UNEXPECTED,
        n_scenarios=job.n_scenarios, started_at=started_at,
    )
    timings = output.timings

    def finish(code: ExitCode) -> JobOutput:
        timings.total_s = time.perf_counter() - t0
        output.ok = code is ExitCode.OK
        output.exit_code = int(code)
        output.finished_at = datetime.now(UTC)
        return output

    log(f"simulating {job.n_scenarios:,} scenarios on backend={job.backend}")
    t = time.perf_counter()
    try:
        run = _simulate_fn()(
            job.decision, n_scenarios=job.n_scenarios, seed=job.seed, backend=job.backend,
            sensitivity=job.sensitivity,
        )
    except Exception as exc:
        timings.simulate_s = time.perf_counter() - t
        output.errors.append(_error("simulate", exc))
        log(f"simulation failed: {exc!r}")
        return finish(ExitCode.SIMULATION_FAILED)
    timings.simulate_s = time.perf_counter() - t
    output.simulation = run.result
    output.backend = run.result.backend
    output.notes.extend(getattr(run, "notes", []) or [])
    log(f"simulation done in {timings.simulate_s:.2f}s on {output.backend}")

    if job.task == "simulate_and_optimize":
        log(f"optimizing with solver={job.solver} budget={job.budget} max_cvar_loss={job.max_cvar_loss}")
        t = time.perf_counter()
        try:
            output.optimization = _optimize_fn()(
                run, job.decision, budget=job.budget, max_cvar_loss=job.max_cvar_loss, solver=job.solver
            )
        except Exception as exc:
            timings.optimize_s = time.perf_counter() - t
            output.errors.append(_error("optimize", exc))
            log(f"optimization failed: {exc!r}")
            return finish(ExitCode.OPTIMIZATION_FAILED)
        timings.optimize_s = time.perf_counter() - t
        log(f"optimization done in {timings.optimize_s:.2f}s")
    return finish(ExitCode.OK)


def write_output(output: JobOutput, path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # One direct write instead of write-then-rename: bucket mounts may not support rename,
    # and readers only look at the file after the job has finished.
    target.write_bytes(output.model_dump_json().encode("utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one job and return the process exit code."""
    environ = os.environ if environ is None else environ
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m variator.cloud.job_entry", description=__doc__)
    parser.add_argument("--input", help=f"job input JSON file (default: ${INPUT_PATH_ENV})")
    parser.add_argument("--output", help=f"where to write the JobOutput JSON (default: ${OUTPUT_PATH_ENV})")
    args = parser.parse_args(argv)
    output_path: str | None = args.output or environ.get(OUTPUT_PATH_ENV) or None

    def log(message: str) -> None:
        print(f"[variator-job] {message}", file=err, flush=True)

    t0 = time.perf_counter()
    started_at = datetime.now(UTC)
    try:
        job = load_input(args.input, environ)
    except _InputError as exc:
        log(str(exc))
        output = JobOutput(
            ok=False, exit_code=int(exc.code), errors=[_error("input", exc)], started_at=started_at,
            finished_at=datetime.now(UTC), timings=JobTimings(load_s=time.perf_counter() - t0),
        )
        output.timings.total_s = output.timings.load_s
        return _report(output, output_path, out, log)
    load_s = time.perf_counter() - t0
    log(f"run {job.run_id}: task={job.task}, input loaded in {load_s:.3f}s")

    try:
        output = execute(job, log=log)
    except Exception as exc:  # a bug in this module, not in the simulation: still report it
        log(traceback.format_exc())
        output = JobOutput(
            run_id=job.run_id, task=job.task, ok=False, exit_code=int(ExitCode.UNEXPECTED),
            errors=[_error("simulate", exc)], started_at=started_at, finished_at=datetime.now(UTC),
        )
    output.timings.load_s = load_s
    output.timings.total_s += load_s
    return _report(output, output_path, out, log)


def _report(output: JobOutput, output_path: str | None, out: TextIO, log: Callable[[str], None]) -> int:
    if output_path:
        t = time.perf_counter()
        try:
            write_output(output, output_path)
            output.timings.write_s = time.perf_counter() - t
            log(f"output written to {output_path}")
        except OSError as exc:
            log(f"cannot write output to {output_path}: {exc!r}")
            output.errors.append(_error("output", exc))
            output.ok = False
            if output.exit_code == ExitCode.OK:
                output.exit_code = int(ExitCode.OUTPUT_FAILED)
            output_path = None
    else:
        log(f"no output path (--output / ${OUTPUT_PATH_ENV}); the result is only in the stdout marker")
    print(format_marker(output, output_path), file=out, flush=True)
    return output.exit_code


if __name__ == "__main__":
    sys.exit(main())
