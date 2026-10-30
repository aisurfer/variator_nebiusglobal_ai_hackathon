"""Public entry points: pick the best portfolio of options across Monte Carlo scenarios."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Literal, Protocol

import numpy as np

from variator.domain import DecisionModel, OptimizationResult
from variator.optimization import cuopt_local, cuopt_server, formulation, highs
from variator.optimization.formulation import (
    MilpFormulation,
    PortfolioProblem,
    build_formulation,
    constraint_violations,
)
from variator.optimization.metrics import evaluate_portfolio
from variator.optimization.outcome import SolverName, SolverOutcome

SolverChoice = Literal["auto", "cuopt", "highs"]
_Backend = Literal["cuopt_local", "cuopt_server", "highs"]
_BACKEND_LABEL = {"cuopt_local": "local GPU", "cuopt_server": "cuOpt server"}


class ScenarioRun(Protocol):
    """What the optimizer needs from ``variator.simulation.SimulationRun``."""

    @property
    def scenarios(self) -> np.ndarray:
        """(n_scenarios, n_options) metric values with common random numbers across options."""
        ...

    @property
    def option_names(self) -> Sequence[str]: ...


def optimize_portfolio(
    scenarios: np.ndarray,
    option_names: Sequence[str],
    *,
    costs: Sequence[float | None] | None = None,
    budget: float | None = None,
    exclusive_groups: Sequence[Sequence[str]] = (),
    cvar_alpha: float = 0.95,
    max_cvar_loss: float | None = None,
    solver: SolverChoice = "auto",
    time_limit_s: float = 30.0,
) -> OptimizationResult:
    """Choose the subset of options that maximizes expected total value across scenarios.

    ``scenarios[s, k]`` is the metric of option ``k`` in scenario ``s``; chosen options add up
    and choosing nothing (status quo, value 0) is allowed. Constraints: total cost of chosen
    options <= ``budget`` (a ``None`` cost counts as 0), at most one option per exclusive
    group, and optionally CVaR at ``cvar_alpha`` of the loss (loss = -portfolio value) <=
    ``max_cvar_loss``. ``result.cvar`` follows ``OptionStats.cvar_95``: the mean portfolio value
    in the worst ``1 - cvar_alpha`` of scenarios, so the limit reads ``cvar >= -max_cvar_loss``.

    ``solver="auto"`` uses local cuOpt when installed with a GPU, else a cuOpt server when
    ``CUOPT_SERVER_URL`` is set, else HiGHS; any cuOpt failure falls back to HiGHS with a note.
    Infeasible problems return ``status="infeasible"`` and an empty selection instead of raising.
    Invalid inputs raise ``ValueError``.
    """
    started = time.perf_counter()
    if solver not in ("auto", "cuopt", "highs"):
        raise ValueError(f"solver must be 'auto', 'cuopt' or 'highs', got {solver!r}")
    if not time_limit_s > 0:
        raise ValueError("time_limit_s must be positive")
    problem = PortfolioProblem.from_inputs(
        scenarios,
        option_names,
        costs=costs,
        budget=budget,
        exclusive_groups=exclusive_groups,
        cvar_alpha=cvar_alpha,
        max_cvar_loss=max_cvar_loss,
    )
    solver_notes: list[str] = []
    milp, outcome = _solve(problem, solver=solver, time_limit_s=time_limit_s, notes=solver_notes)
    notes = [*milp.notes, *solver_notes]
    return _to_result(problem, milp, outcome, notes, time_limit_s=time_limit_s, started=started)


def optimize_decision(
    run: ScenarioRun,
    model: DecisionModel,
    *,
    budget: float | None = None,
    max_cvar_loss: float | None = None,
    solver: SolverChoice = "auto",
) -> OptimizationResult:
    """Optimize over a simulation run, taking option costs and exclusive groups from the model.

    For a lower-is-better metric (a cost) the optimizer minimizes the expected total instead;
    ``expected_value`` and ``cvar`` stay in metric units, ``cvar`` is the mean of the worst
    (highest) tail as in ``OptionStats.cvar_95``, and the limit reads ``cvar <= max_cvar_loss``.
    """
    names = list(run.option_names)
    known = {option.name for option in model.options}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"simulation run has options missing from the decision model: {unknown}")
    present = set(names)
    groups = [[name for name in group if name in present] for group in model.exclusive_groups]
    sign = 1.0 if model.metric.higher_is_better else -1.0
    result = optimize_portfolio(
        sign * np.asarray(run.scenarios, dtype=np.float64),
        names,
        costs=[model.option(name).cost for name in names],
        budget=budget,
        exclusive_groups=groups,
        max_cvar_loss=max_cvar_loss,
        solver=solver,
    )
    if sign > 0:
        return result
    note = (
        f"'{model.metric.name}' is lower-is-better: the optimizer minimized its expected total; "
        "cvar is the average of the worst (highest) tail."
    )
    update = {"expected_value": -result.expected_value, "notes": [*result.notes, note]}
    if result.cvar is not None:
        update["cvar"] = -result.cvar
    return result.model_copy(update=update)


def _choose_backend(solver: SolverChoice, notes: list[str]) -> _Backend:
    if solver == "highs":
        return "highs"
    if cuopt_local.is_available():
        return "cuopt_local"
    if cuopt_server.is_configured():
        return "cuopt_server"
    if solver == "cuopt":
        notes.append(
            "cuOpt was requested but there is neither a local cuOpt install with a GPU nor "
            f"{cuopt_server.SERVER_URL_ENV}; solved with HiGHS instead."
        )
    return "highs"


def _run_cuopt(backend: _Backend, milp: MilpFormulation, time_limit_s: float) -> SolverOutcome:
    if backend == "cuopt_local":
        return cuopt_local.solve_cuopt_local(milp, time_limit_s=time_limit_s)
    return cuopt_server.solve_cuopt_server(milp, time_limit_s=time_limit_s)


def _cuopt_problem(outcome: SolverOutcome, problem: PortfolioProblem, milp: MilpFormulation) -> str | None:
    """Why a cuOpt outcome should not be trusted, or ``None`` when it is usable."""
    if outcome.status == "infeasible":
        if milp.empty_portfolio_feasible:
            return "reported infeasible although choosing nothing is feasible"
        return None
    if outcome.status not in ("optimal", "feasible"):
        return f"returned status {outcome.status!r} ({outcome.message})"
    if outcome.x is None:
        return "returned no solution vector"
    violations = constraint_violations(problem, milp, milp.selection(outcome.x))
    if violations:
        return "returned a portfolio that violates constraints: " + "; ".join(violations)
    return None


def _formulate(problem: PortfolioProblem, solver: SolverName) -> MilpFormulation:
    # Read the module attribute at call time so caps can be tuned (or monkeypatched in tests).
    return build_formulation(problem, max_scenarios=formulation.MAX_CVAR_SCENARIOS[solver])


def _solve(
    problem: PortfolioProblem,
    *,
    solver: SolverChoice,
    time_limit_s: float,
    notes: list[str],
) -> tuple[MilpFormulation, SolverOutcome]:
    backend = _choose_backend(solver, notes)
    if backend != "highs":
        label = _BACKEND_LABEL[backend]
        milp = _formulate(problem, "cuopt")
        try:
            outcome = _run_cuopt(backend, milp, time_limit_s)
        except Exception as exc:  # any cuOpt failure (import, CUDA, HTTP, solver) must fall back
            notes.append(f"cuOpt ({label}) failed: {type(exc).__name__}: {exc}; fell back to HiGHS.")
        else:
            issue = _cuopt_problem(outcome, problem, milp)
            if issue is None:
                notes.append(_cuopt_note(label, outcome))
                return milp, outcome
            notes.append(f"cuOpt ({label}) {issue}; fell back to HiGHS.")
    milp = _formulate(problem, "highs")
    return milp, highs.solve_highs(milp, time_limit_s=time_limit_s)


def _cuopt_note(label: str, outcome: SolverOutcome) -> str:
    gap = "" if outcome.mip_gap is None else f", MIP gap {outcome.mip_gap:.2%}"
    verdict = (
        "it reports the portfolio optimal within its gap tolerance"
        if outcome.proven_optimal
        else "treat the portfolio as near-optimal"
    )
    return f"Solved with NVIDIA cuOpt ({label}{gap}); cuOpt's MILP solver is in beta, {verdict}."


def _to_result(
    problem: PortfolioProblem,
    milp: MilpFormulation,
    outcome: SolverOutcome,
    notes: list[str],
    *,
    time_limit_s: float,
    started: float,
) -> OptimizationResult:
    if outcome.x is None or not outcome.has_solution:
        status = "error" if outcome.status in ("optimal", "feasible") else outcome.status
        notes.append(_no_solution_note(status, outcome.message, time_limit_s))
        return OptimizationResult(
            selected=[],
            expected_value=0.0,
            cvar=None,
            budget_used=None,
            solver=outcome.solver,
            status=status,
            proven_optimal=False,
            elapsed_s=time.perf_counter() - started,
            notes=notes,
        )

    selected = milp.selection(outcome.x)
    violations = constraint_violations(problem, milp, selected)
    if violations:
        notes.append("Solver tolerance issue: " + "; ".join(violations) + ".")
    # Report budget use whenever costs or a budget were given (missing costs count as 0).
    report_costs = problem.costs
    if report_costs is None and problem.budget is not None:
        report_costs = np.zeros(problem.n_options)
    metrics = evaluate_portfolio(problem.scenarios, selected, alpha=problem.cvar_alpha, costs=report_costs)
    sampled = milp.sample_indices is not None and milp.sample_indices.size < problem.n_scenarios
    if sampled and problem.max_cvar_loss is not None and metrics.cvar_loss > problem.max_cvar_loss:
        notes.append(
            f"On all {problem.n_scenarios:,} scenarios the average loss in the worst tail is "
            f"{metrics.cvar_loss:,.2f}, above the limit of {problem.max_cvar_loss:,.2f} met on the sample."
        )
    if outcome.status == "feasible":
        notes.append(f"The solver hit the {time_limit_s:g}s time limit; the portfolio may be suboptimal.")
    return OptimizationResult(
        selected=[name for name, chosen in zip(problem.option_names, selected, strict=True) if chosen],
        expected_value=metrics.expected_value,
        cvar=-metrics.cvar_loss,  # metric-space tail mean, the OptionStats.cvar_95 convention
        cvar_alpha=problem.cvar_alpha,
        budget_used=metrics.budget_used,
        solver=outcome.solver,
        status=outcome.status,
        proven_optimal=outcome.proven_optimal and outcome.status == "optimal" and not violations,
        elapsed_s=time.perf_counter() - started,
        notes=notes,
    )


def _no_solution_note(status: str, message: str, time_limit_s: float) -> str:
    if status == "infeasible":
        return (
            "No portfolio satisfies the constraints (for example the CVaR limit is stricter than the "
            "status quo, or the budget is negative); nothing was selected."
        )
    if status == "time_limit":
        return f"No feasible portfolio was found within the {time_limit_s:g}s limit; nothing was selected."
    return f"The solver failed ({status}: {message}); nothing was selected."
