"""Solver-agnostic MILP for choosing a portfolio of options across Monte Carlo scenarios.

Canonical form shared by HiGHS and cuOpt::

    minimize    c @ z
    subject to  row_lower <= A @ z <= row_upper
                var_lower <= z <= var_upper,   z_j integer where integrality[j] == 1

Variable layout ``z = [x_0 .. x_{K-1}, eta, u_0 .. u_{S'-1}]``: binary option picks ``x``, then,
only when a CVaR limit is set, the Rockafellar–Uryasev auxiliaries ``eta`` (free, ends up at
VaR) and ``u_s >= 0`` (tail excess per sampled scenario).

Rows: a cardinality row (keeps ``A`` non-empty), optional budget, one row per exclusive group,
and, with a CVaR limit, ``V[s] @ x + eta + u_s >= 0`` per sampled scenario plus
``eta + sum(u) / ((1 - alpha) S') <= max_cvar_loss``. Values and costs are divided by their
largest magnitude so solver tolerances act on O(1) numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from variator.optimization.metrics import cvar_of_loss
from variator.optimization.outcome import SolverName

# Largest number of scenario rows in the CVaR block, per solver. The expected value needs no
# rows (it is linear in x), so only the CVaR block grows with S. HiGHS 1.12 (scipy 1.18, one
# core) with 3-8 options: 1,000 rows 0.15-0.8 s, 2,000 rows 0.8-2.4 s, 5,000 rows 1.6-12 s.
# ASSUMPTION: cuOpt copes with 10,000 rows within the default 30 s limit; tune on a GPU box.
MAX_CVAR_SCENARIOS: dict[SolverName, int] = {"highs": 2_000, "cuopt": 10_000}

# Post-solve checks allow for solver feasibility tolerances (~1e-7 on the scaled rows).
_BUDGET_RTOL = 1e-6
_CVAR_RTOL = 1e-6


@dataclass(frozen=True)
class PortfolioProblem:
    """Validated optimization inputs in original units."""

    scenarios: np.ndarray  # (S, K) float64: metric of option k in scenario s
    option_names: tuple[str, ...]
    costs: np.ndarray | None  # (K,), a missing cost counts as 0; None when no costs were given
    budget: float | None
    groups: tuple[tuple[int, ...], ...]  # option indices; at most one pick per group
    cvar_alpha: float
    max_cvar_loss: float | None

    @property
    def n_scenarios(self) -> int:
        return int(self.scenarios.shape[0])

    @property
    def n_options(self) -> int:
        return int(self.scenarios.shape[1])

    @classmethod
    def from_inputs(
        cls,
        scenarios: np.ndarray,
        option_names: Sequence[str],
        *,
        costs: Sequence[float | None] | None = None,
        budget: float | None = None,
        exclusive_groups: Sequence[Sequence[str]] = (),
        cvar_alpha: float = 0.95,
        max_cvar_loss: float | None = None,
    ) -> PortfolioProblem:
        """Validate raw inputs; raise ``ValueError`` on shape, name or range problems."""
        matrix = np.asarray(scenarios, dtype=np.float64)
        names = tuple(option_names)
        if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] == 0:
            raise ValueError(f"scenarios must be a non-empty (n_scenarios, n_options) matrix: {matrix.shape}")
        if matrix.shape[1] != len(names):
            raise ValueError(f"scenarios has {matrix.shape[1]} columns but {len(names)} option names")
        if len(set(names)) != len(names):
            raise ValueError("option names must be unique")
        if not np.all(np.isfinite(matrix)):
            raise ValueError("scenarios contain NaN or infinite values")

        cost_array = None
        if costs is not None:
            if len(costs) != len(names):
                raise ValueError(f"got {len(costs)} costs for {len(names)} options")
            cost_array = np.array([0.0 if c is None else float(c) for c in costs], dtype=np.float64)
            if not np.all(np.isfinite(cost_array)):
                raise ValueError("costs must be finite")
        if budget is not None and not np.isfinite(budget):
            raise ValueError("budget must be finite")
        if not 0.0 < cvar_alpha < 1.0:
            raise ValueError(f"cvar_alpha must be in (0, 1), got {cvar_alpha}")
        if max_cvar_loss is not None and not np.isfinite(max_cvar_loss):
            raise ValueError("max_cvar_loss must be finite")

        index = {name: i for i, name in enumerate(names)}
        groups: list[tuple[int, ...]] = []
        for group in exclusive_groups:
            unknown = [name for name in group if name not in index]
            if unknown:
                raise ValueError(f"exclusive group references unknown options: {unknown}")
            members = tuple(sorted({index[name] for name in group}))
            if len(members) > 1:  # a single binary is trivially "at most one"
                groups.append(members)

        return cls(
            scenarios=matrix,
            option_names=names,
            costs=cost_array,
            budget=None if budget is None else float(budget),
            groups=tuple(groups),
            cvar_alpha=float(cvar_alpha),
            max_cvar_loss=None if max_cvar_loss is None else float(max_cvar_loss),
        )


@dataclass(frozen=True)
class MilpFormulation:
    """MILP arrays in canonical minimize form plus the bookkeeping to read solutions back."""

    c: np.ndarray
    a: sp.csr_matrix
    row_lower: np.ndarray
    row_upper: np.ndarray
    var_lower: np.ndarray
    var_upper: np.ndarray
    integrality: np.ndarray  # 1 = integer (binary given its bounds), 0 = continuous
    n_options: int
    eta_index: int | None
    u_slice: slice | None
    sample_indices: np.ndarray | None  # scenario rows in the CVaR block; None = no CVaR block
    value_scale: float
    notes: tuple[str, ...] = ()

    @property
    def n_vars(self) -> int:
        return int(self.c.size)

    @property
    def n_rows(self) -> int:
        return int(self.a.shape[0])

    @property
    def empty_portfolio_feasible(self) -> bool:
        """Whether z = 0 (choose nothing, eta = u = 0) satisfies every row.

        Used to reject a solver's "infeasible" claim that cannot be true.
        """
        return bool(np.all(self.row_lower <= 0.0) and np.all(self.row_upper >= 0.0))

    def selection(self, z: np.ndarray) -> np.ndarray:
        """Boolean mask of chosen options from a solver's variable vector."""
        return np.asarray(z, dtype=np.float64)[: self.n_options] > 0.5


def sample_indices(n_scenarios: int, max_scenarios: int) -> np.ndarray | None:
    """Deterministic, evenly spaced scenario subsample; ``None`` when all scenarios fit.

    Even spacing over i.i.d. Monte Carlo draws is as good as a random subsample and doubles as
    stratification when the input happens to be ordered.
    """
    if max_scenarios < 1:
        raise ValueError("max_scenarios must be >= 1")
    if n_scenarios <= max_scenarios:
        return None
    return np.unique(np.linspace(0, n_scenarios - 1, max_scenarios).round().astype(np.int64))


def build_formulation(
    problem: PortfolioProblem, *, max_scenarios: int = MAX_CVAR_SCENARIOS["highs"]
) -> MilpFormulation:
    """Translate a ``PortfolioProblem`` into canonical MILP arrays (see module docstring)."""
    k = problem.n_options
    v = problem.scenarios
    value_scale = float(np.max(np.abs(v))) or 1.0
    notes: list[str] = []

    with_cvar = problem.max_cvar_loss is not None
    sample: np.ndarray | None = None
    n_sample = 0
    if with_cvar:
        sample = sample_indices(problem.n_scenarios, max_scenarios)
        if sample is not None:
            notes.append(
                f"The CVaR limit was enforced on a deterministic, evenly spaced sample of {sample.size:,} "
                f"of {problem.n_scenarios:,} scenarios to keep the MILP small; expected value, CVaR and "
                f"budget are recomputed on all {problem.n_scenarios:,} scenarios."
            )
        else:
            sample = np.arange(problem.n_scenarios)
        n_sample = int(sample.size)
    n_vars = k + (1 + n_sample if with_cvar else 0)

    c = np.zeros(n_vars)
    c[:k] = -v.mean(axis=0) / value_scale  # maximize E[value] == minimize -E[value]

    # Option-only rows: cardinality, budget, exclusive groups.
    rows: list[np.ndarray] = [np.ones(k)]
    lower: list[float] = [0.0]
    upper: list[float] = [float(k)]
    if problem.budget is not None:
        costs = problem.costs if problem.costs is not None else np.zeros(k)
        if problem.costs is None:
            notes.append("A budget was given without option costs, so it cannot bind.")
        cost_scale = float(max(np.max(np.abs(costs)), abs(problem.budget))) or 1.0
        rows.append(costs / cost_scale)
        lower.append(-np.inf)
        upper.append(problem.budget / cost_scale)
    for group in problem.groups:
        row = np.zeros(k)
        row[list(group)] = 1.0
        rows.append(row)
        lower.append(-np.inf)
        upper.append(1.0)
    option_block = sp.csr_matrix(np.vstack(rows))
    if with_cvar:
        option_block = sp.hstack([option_block, sp.csr_matrix((len(rows), 1 + n_sample))], format="csr")
    blocks = [option_block]

    eta_index: int | None = None
    u_slice: slice | None = None
    var_lower = np.zeros(n_vars)
    var_upper = np.ones(n_vars)
    integrality = np.zeros(n_vars, dtype=np.int64)
    integrality[:k] = 1

    if with_cvar:
        assert sample is not None and problem.max_cvar_loss is not None
        eta_index = k
        u_slice = slice(k + 1, k + 1 + n_sample)
        var_lower[eta_index] = -np.inf
        var_upper[eta_index:] = np.inf
        # u_s >= loss_s - eta with loss_s = -V[s] @ x   <=>   V[s] @ x + eta + u_s >= 0
        tail_rows = sp.hstack(
            [
                sp.csr_matrix(v[sample] / value_scale),
                sp.csr_matrix(np.ones((n_sample, 1))),
                sp.identity(n_sample, format="csr"),
            ],
            format="csr",
        )
        limit_row = np.zeros((1, n_vars))
        limit_row[0, eta_index] = 1.0
        limit_row[0, u_slice] = 1.0 / ((1.0 - problem.cvar_alpha) * n_sample)
        blocks += [tail_rows, sp.csr_matrix(limit_row)]
        lower += [0.0] * n_sample + [-np.inf]
        upper += [np.inf] * n_sample + [problem.max_cvar_loss / value_scale]

    a = sp.vstack(blocks, format="csr")
    a.sort_indices()
    return MilpFormulation(
        c=c,
        a=a,
        row_lower=np.asarray(lower, dtype=np.float64),
        row_upper=np.asarray(upper, dtype=np.float64),
        var_lower=var_lower,
        var_upper=var_upper,
        integrality=integrality,
        n_options=k,
        eta_index=eta_index,
        u_slice=u_slice,
        sample_indices=sample,
        value_scale=value_scale,
        notes=tuple(notes),
    )


def constraint_violations(
    problem: PortfolioProblem, formulation: MilpFormulation, selected: np.ndarray
) -> list[str]:
    """Check a selection against the original constraints in original units.

    The CVaR limit is checked on the same scenario sample the MILP used. Returns human-readable
    violations (empty when the selection is feasible).
    """
    issues: list[str] = []
    picks = selected.astype(np.float64)
    if problem.budget is not None and problem.costs is not None:
        spent = float(problem.costs @ picks)
        scale = max(1.0, abs(problem.budget), float(np.max(np.abs(problem.costs))))
        if spent > problem.budget + _BUDGET_RTOL * scale:
            issues.append(f"budget exceeded ({spent:,.2f} > {problem.budget:,.2f})")
    for group in problem.groups:
        chosen = [problem.option_names[i] for i in group if selected[i]]
        if len(chosen) > 1:
            issues.append(f"more than one option from an exclusive group: {chosen}")
    if problem.max_cvar_loss is not None and formulation.sample_indices is not None:
        losses = -(problem.scenarios[formulation.sample_indices] @ picks)
        cvar = cvar_of_loss(losses, problem.cvar_alpha)
        if cvar > problem.max_cvar_loss + _CVAR_RTOL * formulation.value_scale:
            issues.append(f"CVaR limit exceeded ({cvar:,.2f} > {problem.max_cvar_loss:,.2f})")
    return issues
