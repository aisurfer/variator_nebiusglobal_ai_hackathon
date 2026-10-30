"""Keyed random streams for event-level common random numbers (04-ANSWERS §1.6).

Each source of randomness has its own stream. A label names the stream, for example
``s17:churn:contract-042:time``. A stream does not depend on other streams, so a
decision that changes one risk does not move the draws of other entities.

Three methods use these streams:

- named streams: ``Streams(seed).rng(*parts)``;
- cumulative-hazard inversion: one ``E ~ Exp(1)`` per entity and purpose, and the
  event happens in the first period where the cumulative hazard reaches ``E``
  (``first_passage``); a higher hazard never gives a later event;
- slots for conditional entities (``hire-slot-3``) and child keys for tree nodes
  (``<parent>/<j>``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from variator.synthetic.company import derived_rng

SEP = ":"
CHILD_SEP = "/"
NEVER = -1  # first_passage result when the hazard never reaches E


def _part(part: object) -> str:
    """One label part. '%' and ':' are escaped, so different keys give different labels."""
    text = str(part)
    if not text:
        raise ValueError("a key part must not be empty")
    return text.replace("%", "%25").replace(SEP, "%3A")


def stream_label(*parts: object) -> str:
    """Join key parts into a stream label: ``stream_label("s17", "churn", "c-42", "time")``."""
    if not parts:
        raise ValueError("a stream label needs at least one part")
    return SEP.join(_part(p) for p in parts)


def scenario_part(scenario: int) -> str:
    """The scenario part of a label: ``s17``."""
    return f"s{int(scenario)}"


def slot_key(kind: str, n: int) -> str:
    """Key of the n-th conditional entity of a kind, 1-based: ``slot_key("hire", 3) == "hire-slot-3"``.

    The third hired engineer has the same key (so the same draws) in every option that
    hires at least three engineers, whatever the month of the hire.
    """
    if n < 1:
        raise ValueError("slot numbers start at 1")
    return f"{kind}-slot-{int(n)}"


def slot_keys(kind: str, count: int, start: int = 1) -> list[str]:
    """Keys of ``count`` slots from ``start``: ``hire-slot-1 .. hire-slot-<count>``."""
    return [slot_key(kind, n) for n in range(start, start + count)]


def child_key(parent: str, j: int) -> str:
    """Key of child ``j`` of tree node ``parent``: ``child_key("tree/0", 2) == "tree/0/2"``."""
    if j < 0:
        raise ValueError("child numbers start at 0")
    return f"{parent}{CHILD_SEP}{int(j)}"


class Streams:
    """Named random streams for one seed, with an optional scope prefix.

    ``Streams(7).rng("s17", "churn", "contract-042", "time")`` is the same stream as
    ``derived_rng(7, "s17:churn:contract-042:time")``.
    """

    def __init__(self, seed: int, scope: Sequence[object] = ()):
        self.seed = int(seed)
        self.scope = tuple(str(p) for p in scope)

    def __repr__(self) -> str:
        return f"Streams(seed={self.seed}, scope={self.scope!r})"

    def label(self, *parts: object) -> str:
        return stream_label(*self.scope, *parts)

    def rng(self, *parts: object) -> np.random.Generator:
        return derived_rng(self.seed, self.label(*parts))

    def scoped(self, *parts: object) -> Streams:
        """The same seed with a longer scope (for example a tree node key)."""
        return Streams(self.seed, self.scope + tuple(str(p) for p in parts))

    def event_rng(self, scenario: int, process: str, entity: str, purpose: str) -> np.random.Generator:
        """The stream ``s{scenario}:{process}:{entity}:{purpose}`` of the design (§1.6)."""
        return self.rng(scenario_part(scenario), process, entity, purpose)

    def _draws(
        self,
        process: str,
        entities: Iterable[str],
        purpose: str,
        scenarios: int | Sequence[int],
        count: int | None,
        draw: str,
    ) -> np.ndarray:
        ids = list(entities)
        rows = range(scenarios) if isinstance(scenarios, int) else [int(s) for s in scenarios]
        n = 1 if count is None else int(count)
        if n < 1:
            raise ValueError("count must be >= 1")
        out = np.empty((len(rows), len(ids), n), dtype=np.float64)
        for i, s in enumerate(rows):
            for j, entity in enumerate(ids):
                out[i, j] = getattr(self.event_rng(s, process, entity, purpose), draw)(n)
        return out[..., 0] if count is None else out

    def unit_exponentials(
        self,
        process: str,
        entities: Iterable[str],
        purpose: str,
        scenarios: int | Sequence[int],
        count: int | None = None,
    ) -> np.ndarray:
        """E ~ Exp(1) per scenario and entity: shape (S, N), or (S, N, count).

        ``scenarios`` is a number S (scenarios 0..S-1) or a list of scenario numbers.
        Each value is the first draw (or the first ``count`` draws) of the stream
        ``s{s}:{process}:{entity}:{purpose}``. Thus the values of one entity in one
        scenario do not change when other entities, scenarios or a larger ``count``
        are requested. Cost: about 6 microseconds per stream.
        """
        return self._draws(process, entities, purpose, scenarios, count, "standard_exponential")

    def uniforms(
        self,
        process: str,
        entities: Iterable[str],
        purpose: str,
        scenarios: int | Sequence[int],
        count: int | None = None,
    ) -> np.ndarray:
        """U ~ Uniform(0, 1) per scenario and entity, with the same keys as ``unit_exponentials``."""
        return self._draws(process, entities, purpose, scenarios, count, "random")


def cumulative_hazard(hazard: np.ndarray, axis: int = -1) -> np.ndarray:
    """Cumulative hazard at the end of each period from the hazard of each period."""
    h = np.asarray(hazard, dtype=np.float64)
    if (h < 0).any():
        raise ValueError("a hazard must not be negative")
    return np.cumsum(h, axis=axis)


def hazard_of_probability(p: np.ndarray | float) -> np.ndarray:
    """Hazard of one period from the probability that the event happens in it: -log(1 - p)."""
    q = np.asarray(p, dtype=np.float64)
    if ((q < 0) | (q > 1)).any():
        raise ValueError("a probability must be in [0, 1]")
    with np.errstate(divide="ignore"):
        return -np.log1p(-q)


def first_passage(cum_hazard: np.ndarray, e: np.ndarray | float) -> np.ndarray:
    """First period index where the cumulative hazard reaches E; ``NEVER`` (-1) if never.

    ``cum_hazard`` has shape (..., T): the cumulative hazard at the end of each period.
    ``e`` broadcasts against ``cum_hazard.shape[:-1]`` (for example (S, 1, N) against
    (S, O, N, T) shares E over options). The result has the broadcast shape.
    """
    h = np.asarray(cum_hazard, dtype=np.float64)
    x = np.asarray(e, dtype=np.float64)
    hit = h >= x[..., None]
    first = hit.argmax(axis=-1)
    return np.where(hit.any(axis=-1), first, NEVER).astype(np.int64)


def arrival_counts(cum_hazard: np.ndarray, e_seq: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Events per period of a counting process, from a unit Poisson process (time change).

    Arrival k happens in the first period where the cumulative hazard reaches
    E_1 + ... + E_k. ``e_seq`` has shape (..., K) and broadcasts against
    ``cum_hazard.shape[:-1]``. Returns ``(counts, saturated)``: counts per period
    (..., T), and True where all K arrivals happened (request a larger K to be exact).
    """
    h = np.asarray(cum_hazard, dtype=np.float64)
    thresholds = np.cumsum(np.asarray(e_seq, dtype=np.float64), axis=-1)
    n_by = (thresholds[..., None, :] <= h[..., :, None]).sum(axis=-1)
    counts = np.diff(n_by, axis=-1, prepend=0)
    return counts.astype(np.int64), n_by[..., -1] >= thresholds.shape[-1]
