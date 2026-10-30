"""Gaussian copula: impose pairwise rank (Spearman) correlations on uniform columns.

Pipeline: uniforms -> standard normals (ndtri) -> multiply by the Cholesky factor of the
Pearson correlation matrix -> back to uniforms (ndtr). Marginals are applied afterwards
by inverse CDF, which preserves ranks, so the achieved Spearman correlation equals the
target for continuous marginals.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from variator.domain import Correlation
from variator.simulation.backend import Backend

# Smallest eigenvalue kept when repairing a matrix that is not positive definite.
MIN_EIGENVALUE = 1e-6
_U_TINY = 1e-12


class CorrelationRepairWarning(UserWarning):
    """The requested correlations were inconsistent and had to be adjusted."""


def spearman_to_pearson(rho: float) -> float:
    """Pearson correlation of two normals whose Spearman rank correlation is ``rho``."""
    return 2 * math.sin(math.pi * rho / 6)


def is_positive_definite(matrix: np.ndarray) -> bool:
    """True if ``matrix`` admits a Cholesky factorization."""
    try:
        np.linalg.cholesky(matrix)
    except np.linalg.LinAlgError:
        return False
    return True


def nearest_positive_definite(matrix: np.ndarray, min_eigenvalue: float = MIN_EIGENVALUE) -> np.ndarray:
    """Repair a symmetric matrix into a positive-definite correlation matrix.

    Clips eigenvalues at ``min_eigenvalue`` and rescales back to a unit diagonal. This is
    the simple eigenvalue-clipping repair, not Higham's (slower) nearest correlation matrix.
    """
    sym = (matrix + matrix.T) / 2
    values, vectors = np.linalg.eigh(sym)
    clipped = (vectors * np.maximum(values, min_eigenvalue)) @ vectors.T
    scale = 1 / np.sqrt(np.diag(clipped))
    repaired = clipped * np.outer(scale, scale)
    np.fill_diagonal(repaired, 1.0)
    return repaired


@dataclass(frozen=True)
class GaussianCopula:
    """Correlation structure over the subset of variables that appear in correlations."""

    names: list[str]
    indices: list[int]  # column of each name in the full uniform matrix
    target: np.ndarray  # requested Spearman matrix over ``names``
    pearson: np.ndarray  # normal-space correlation actually used (after any repair)
    lower: np.ndarray  # Cholesky factor of ``pearson``
    notes: list[str] = field(default_factory=list)

    def apply(self, u: Any, backend: Backend) -> Any:
        """Return a copy of uniform matrix ``u`` (scenarios x variables) with correlated columns."""
        xp = backend.xp
        cols = xp.asarray(self.indices)
        z = backend.ndtri(xp.clip(u[:, cols], _U_TINY, 1 - _U_TINY))
        correlated = z @ backend.asarray(self.lower.T)
        out = u.copy()
        out[:, cols] = backend.ndtr(correlated)
        return out


def build_copula(variable_names: Sequence[str], correlations: Sequence[Correlation]) -> GaussianCopula | None:
    """Assemble the correlation matrix, repair it if needed and factor it.

    Returns None when there are no correlations. Duplicate pairs keep the last value
    (with a note). A non-positive-definite matrix is repaired with a
    ``CorrelationRepairWarning`` and a note describing the largest change.
    """
    if not correlations:
        return None
    position = {name: i for i, name in enumerate(variable_names)}
    names = sorted({c.a for c in correlations} | {c.b for c in correlations}, key=position.__getitem__)
    local = {name: i for i, name in enumerate(names)}
    notes: list[str] = []
    target = np.eye(len(names))
    seen: dict[frozenset[str], float] = {}
    for corr in correlations:
        pair = frozenset((corr.a, corr.b))
        if pair in seen and seen[pair] != corr.rho:
            notes.append(f"correlation {corr.a}/{corr.b} given twice; using rho={corr.rho}")
        seen[pair] = corr.rho
        i, j = local[corr.a], local[corr.b]
        target[i, j] = target[j, i] = corr.rho

    pearson = 2 * np.sin(np.pi * target / 6)
    np.fill_diagonal(pearson, 1.0)
    if not is_positive_definite(pearson):
        repaired = nearest_positive_definite(pearson)
        change = float(np.max(np.abs(repaired - pearson)))
        message = (
            "correlations are mutually inconsistent (matrix not positive definite); "
            f"repaired by eigenvalue clipping, largest change {change:.3f}"
        )
        warnings.warn(message, CorrelationRepairWarning, stacklevel=2)
        notes.append(message)
        pearson = repaired
    return GaussianCopula(
        names=names,
        indices=[position[n] for n in names],
        target=target,
        pearson=pearson,
        lower=np.linalg.cholesky(pearson),
        notes=notes,
    )
