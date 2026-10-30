"""Management layer of a generated company history (VARIATOR orgsim delivery).

The engine finds the moments that managers talk about (incidents, weeks, months, quarters,
client escalations, hiring decisions), plans the artifacts with exact facts, and renders
them as timeline lines in the payload formats of the base delivery. Headless Nemotron
writers write only the prose; a checker rejects invented facts. The overlay is merged with
the base delivery into a new directory.

    plan   -> <work>/plan.json        (artifacts, facts packets, structure)
    write  -> <work>/requests, <work>/responses   (resumable)
    check  -> <work>/accepted.json, <work>/dropped.json
    apply  -> <out>/delivery (merged), <out>/datasets, <out>/truth.json

Modules: ``base`` (read the history), ``planner`` (triggers and schedule), ``facts``
(packets), ``render`` (lines and gold links), ``writers`` (requests, nemotron -p),
``check`` (fact checker), ``apply`` (merge and write), ``cli``.
"""

from __future__ import annotations

__all__ = ["OVERLAY_VERSION"]

OVERLAY_VERSION = "narrative/1"
