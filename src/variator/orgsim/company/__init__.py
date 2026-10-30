"""One year of history of the MVP company (Larchwood Systems): spec, habits, processes, generator.

Use ``generate_history(spec, anchor, seed)`` and ``write_history(history, out_dir)``; CLI
``python -m variator.orgsim.company.cli generate``. See ``README.md``.
"""

from variator.orgsim.company.generator import History, generate_history, write_history
from variator.orgsim.company.spec import CompanySpec, Truth, default_spec

__all__ = ["CompanySpec", "History", "Truth", "default_spec", "generate_history", "write_history"]
