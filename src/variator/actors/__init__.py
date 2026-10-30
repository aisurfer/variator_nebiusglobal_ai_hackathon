"""Learning the company from its history: actors, habits, cohorts, risks, card facts (09 §2, 04 §1.4).

Entry points: ``learn.learn`` (in memory), ``artifacts.write_set`` (artifact set),
``recovery.recovery`` (check against ``truth.json``), CLI ``python -m variator.actors.cli learn``.
"""

from variator.actors.history import History, load_history
from variator.actors.stats import Param

__all__ = ["History", "Param", "load_history"]
