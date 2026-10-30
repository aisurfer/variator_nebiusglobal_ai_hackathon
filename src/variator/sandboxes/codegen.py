"""Agentic simulation code generation: Nemotron writes, the sandbox runs, we check, it fixes.

``generate_and_run`` asks the model for a self-contained NumPy Monte Carlo
program for a ``DecisionModel``, executes it with networking off, validates
``output.json`` against ``SimulationOutput`` and feeds any failure (traceback,
timeout, missing or invalid output) back to the model, up to ``max_iterations``.
Every attempt is kept for the UI.
"""

from __future__ import annotations

import json
import re
import traceback
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from variator.domain import DecisionModel
from variator.llm import TokenFactoryClient, split_reasoning
from variator.sandboxes import prompts
from variator.sandboxes.client import SandboxesClient
from variator.sandboxes.errors import SandboxError
from variator.sandboxes.runner import CODE_FILE, OutputMode, SandboxRunResult, run_python

FailureKind = Literal["no_code", "syntax", "exit", "timeout", "output", "schema", "sandbox"]

_FENCE_RE = re.compile(
    r"^[ \t]*(?P<fence>```|~~~)[ \t]*(?P<lang>[\w+.-]*)[^\n]*\n(?P<body>.*?)^[ \t]*(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_OPEN_FENCE_RE = re.compile(
    r"^[ \t]*```[ \t]*(?:python3?|py)?[ \t]*\n(?P<body>.*)\Z", re.DOTALL | re.MULTILINE
)
_PY_LANGS = frozenset({"python", "python3", "py"})
_TAIL_STDERR = 3000
_TAIL_STDOUT = 1000


class OptionOutcome(BaseModel):
    """Metric distribution summary for one option, as written by generated code."""

    model_config = ConfigDict(allow_inf_nan=False)

    mean: float
    p5: float
    p50: float
    p95: float
    prob_loss: float = Field(ge=0, le=1, description="P(metric < 0)")

    @model_validator(mode="after")
    def _ordered(self) -> OptionOutcome:
        tol = 1e-9 * max(1.0, abs(self.p5), abs(self.p95))
        if self.p5 > self.p50 + tol or self.p50 > self.p95 + tol:
            raise ValueError("percentiles must satisfy p5 <= p50 <= p95")
        return self


class SimulationOutput(BaseModel):
    """Required content of ``output.json``."""

    n_scenarios: int | None = Field(default=None, ge=1)
    options: dict[str, OptionOutcome] = Field(min_length=1)
    notes: str | None = None


class OutputValidationError(ValueError):
    """``output.json`` parsed but does not match the decision's options."""


class CodegenAttempt(BaseModel):
    """One write -> run -> check round, for the UI transcript."""

    iteration: int
    code: str | None
    failure: FailureKind | None = None
    error: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    duration_s: float | None = None
    operation_id: str | None = None
    output_json: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


class CodegenResult(BaseModel):
    success: bool
    output: SimulationOutput | None = None
    code: str | None = Field(default=None, description="Program that produced the output, else the last one")
    attempts: list[CodegenAttempt] = Field(default_factory=list)
    model: str
    aborted: str | None = Field(default=None, description="Sandbox/platform error that stopped the loop")

    @property
    def iterations(self) -> int:
        return len(self.attempts)


def extract_code(text: str) -> str | None:
    """Pull the Python program out of a model reply.

    Prefers ``python``-tagged fenced blocks, then untagged ones (longest wins, the
    later one on ties); accepts a block whose closing fence was cut off; finally
    accepts a bare reply if it compiles and imports something. Returns None otherwise.
    """
    text, _ = split_reasoning(text.replace("\r\n", "\n"))
    blocks = [(m["lang"].lower(), m["body"]) for m in _FENCE_RE.finditer(text)]
    candidates = [body for lang, body in blocks if lang in _PY_LANGS] or [
        body for lang, body in blocks if not lang
    ]
    if candidates:
        best = max(enumerate(candidates), key=lambda item: (len(item[1].strip()), item[0]))[1]
        return _normalize(best)
    if blocks:
        return None  # only json/bash/... blocks
    unterminated = _OPEN_FENCE_RE.search(text)
    if unterminated:
        return _normalize(unterminated["body"])
    bare = text.strip()
    if "import" in bare and _compiles(bare):
        return _normalize(bare)
    return None


def validate_output(raw: dict[str, Any], decision: DecisionModel) -> SimulationOutput:
    """Validate ``output.json`` content; raise ``ValueError`` with a readable message."""
    try:
        output = SimulationOutput.model_validate(raw)
    except ValidationError as exc:
        raise OutputValidationError(_format_validation(exc)) from exc
    expected = [option.name for option in decision.options]
    missing = [name for name in expected if name not in output.options]
    unknown = sorted(set(output.options) - set(expected))
    problems = []
    if missing:
        problems.append(f"missing options: {missing}")
    if unknown:
        problems.append(f"unknown options: {unknown} (use exactly {expected})")
    if problems:
        raise OutputValidationError("; ".join(problems))
    return output


def build_task_prompt(
    decision: DecisionModel, task: str, *, n_scenarios: int, seed: int, timeout_s: int
) -> str:
    """User turn describing the decision, the extra modelling task and the output schema."""
    return prompts.CODEGEN_TASK.format(
        question=decision.question,
        metric_name=decision.metric.name,
        metric_unit=decision.metric.unit,
        task=task.strip() or "Nothing extra: simulate the decision model as specified.",
        semantics=prompts.DECISION_SEMANTICS,
        decision_json=_decision_json(decision),
        n_scenarios=n_scenarios,
        seed=seed,
        timeout_s=timeout_s,
        option_lines=",\n".join(prompts.OPTION_LINE.format(name=o.name) for o in decision.options),
    )


def generate_and_run(
    tf_client: TokenFactoryClient,
    sandbox: SandboxesClient,
    decision: DecisionModel,
    *,
    task: str,
    model: str = "ultra",
    max_iterations: int = 3,
    n_scenarios: int = 20_000,
    seed: int = 42,
    timeout_s: int = 120,
    image: str | None = None,
    output_mode: OutputMode = "artifact",
    reasoning: bool | None = None,
    max_tokens: int | None = None,
    on_attempt: Callable[[CodegenAttempt], None] | None = None,
) -> CodegenResult:
    """Let Nemotron write a custom simulation and iterate until its output validates.

    The generated program always runs with networking disabled. Sandbox/platform
    errors (no Beta access, quota, FAILED operations) stop the loop and are
    reported in ``CodegenResult.aborted`` rather than raised, so callers can fall
    back to the built-in Monte Carlo engine while still showing the transcript.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    inputs = {
        "decision.json": _decision_json(decision),
        "config.json": json.dumps({"n_scenarios": n_scenarios, "seed": seed}),
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.CODEGEN_SYSTEM},
        {
            "role": "user",
            "content": build_task_prompt(
                decision, task, n_scenarios=n_scenarios, seed=seed, timeout_s=timeout_s
            ),
        },
    ]
    attempts: list[CodegenAttempt] = []
    last_code: str | None = None

    def finish(**kwargs: Any) -> CodegenResult:
        return CodegenResult(attempts=attempts, model=tf_client.models.model_id(model), **kwargs)

    for iteration in range(1, max_iterations + 1):
        reply = tf_client.chat(model, messages, reasoning=reasoning, max_tokens=max_tokens)
        code = extract_code(reply.content)
        attempt = CodegenAttempt(iteration=iteration, code=code)
        details = ""

        if code is None:
            attempt.failure, attempt.error = "no_code", prompts.NO_CODE_FAILURE
            assistant_turn = reply.content
        else:
            last_code = code
            assistant_turn = f"```python\n{code}```"
            syntax_error = _syntax_error(code)
            if syntax_error:
                attempt.failure, attempt.error = "syntax", "the program does not compile."
                details = f"```\n{syntax_error}\n```"
            else:
                try:
                    run = run_python(
                        sandbox,
                        code,
                        inputs=inputs,
                        timeout_s=timeout_s,
                        network=False,
                        image=image,
                        output_mode=output_mode,
                    )
                except SandboxError as exc:
                    attempt.failure, attempt.error = "sandbox", f"{type(exc).__name__}: {exc}"
                    _record(attempts, attempt, on_attempt)
                    return finish(success=False, code=last_code, aborted=attempt.error)
                details, output = _check_run(attempt, run, decision, timeout_s)
                if output is not None:
                    _record(attempts, attempt, on_attempt)
                    return finish(success=True, output=output, code=code)

        _record(attempts, attempt, on_attempt)
        messages.append({"role": "assistant", "content": assistant_turn})
        messages.append(
            {
                "role": "user",
                "content": prompts.CODEGEN_FIX.format(
                    attempt=iteration, failure=attempt.error, details=details
                ).strip()
                + "\n",
            }
        )

    return finish(success=False, code=last_code)


def _check_run(
    attempt: CodegenAttempt, run: SandboxRunResult, decision: DecisionModel, timeout_s: int
) -> tuple[str, SimulationOutput | None]:
    """Fill the attempt from a run; return (failure details for the model, validated output)."""
    attempt.exit_code = run.exit_code
    attempt.timed_out = run.timed_out
    attempt.stdout = _tail(run.stdout, _TAIL_STDOUT * 4)
    attempt.stderr = _tail(run.stderr, _TAIL_STDERR * 2)
    attempt.duration_s = run.duration_s
    attempt.operation_id = run.operation_id
    attempt.output_json = run.output_json
    streams = _streams_excerpt(run)

    if run.timed_out:
        attempt.failure = "timeout"
        attempt.error = f"the sandbox killed the program after the {timeout_s}-second time limit."
        return streams + "\nMake it faster: vectorise with NumPy and avoid Python loops over scenarios.", None
    if run.exit_code != 0:
        attempt.failure = "exit"
        how = f"was killed by signal {run.signal} (possibly out of memory)" if run.signal else (
            f"exited with status {run.exit_code}"
        )
        attempt.error = f"the program {how}."
        return streams, None
    if run.output_json is None:
        attempt.failure = "output"
        attempt.error = run.output_error or "output.json was not produced."
        return streams, None
    try:
        output = validate_output(run.output_json, decision)
    except ValueError as exc:
        attempt.failure = "schema"
        attempt.error = "output.json does not match the required schema."
        received = json.dumps(run.output_json)[:1500]
        return f"Problems:\n{exc}\n\nReceived output.json (truncated):\n```json\n{received}\n```", None
    return "", output


def _record(
    attempts: list[CodegenAttempt],
    attempt: CodegenAttempt,
    on_attempt: Callable[[CodegenAttempt], None] | None,
) -> None:
    attempts.append(attempt)
    if on_attempt is not None:
        on_attempt(attempt)


def _streams_excerpt(run: SandboxRunResult) -> str:
    parts = []
    if run.stderr.strip():
        parts.append(f"stderr (last lines):\n```\n{_tail(run.stderr, _TAIL_STDERR)}\n```")
    if run.stdout.strip():
        parts.append(f"stdout (last lines):\n```\n{_tail(run.stdout, _TAIL_STDOUT)}\n```")
    return "\n\n".join(parts) or "The program produced no output."


def _syntax_error(code: str) -> str | None:
    try:
        compile(code, CODE_FILE, "exec")
    except (SyntaxError, ValueError) as exc:
        return "".join(traceback.format_exception_only(exc)).strip()
    return None


def _compiles(code: str) -> bool:
    return _syntax_error(code) is None


def _normalize(code: str) -> str:
    return code.strip("\n") + "\n"


def _tail(text: str, limit: int) -> str:
    text = text.rstrip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _decision_json(decision: DecisionModel) -> str:
    return decision.model_dump_json(indent=2, exclude_none=True)


def _format_validation(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors()[:20]:
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"- {location}: {err['msg']}")
    return "\n".join(lines)
