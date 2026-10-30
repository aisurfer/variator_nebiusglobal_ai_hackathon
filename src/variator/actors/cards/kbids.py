"""The entity ids of the knowledge base (what a citation may point at), with their titles.

``fetch(api)`` pages ``GET /api/v1/entities`` of the KB API as an admin principal (read-only);
``load``/``save`` keep a copy in the work directory, so a re-run of the checker needs no stack.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

PAGE = 500


def fetch(
    api: str, tenant: str = "company", principal: str = "user:admin", timeout: int = 30
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    offset = 0
    while True:
        url = f"{api.rstrip('/')}/api/v1/entities?limit={PAGE}&offset={offset}"
        req = urllib.request.Request(url, headers={"X-KB-Principal": principal, "X-KB-Tenant": tenant})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            items = json.loads(res.read())["items"]
        for it in items:
            out[it["entity_id"]] = {"title": it.get("title") or it["entity_id"], "kind": it.get("kind") or ""}
        if len(items) < PAGE:
            return out
        offset += PAGE


def save(ids: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(ids, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def load(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def short(entity_id: str, known: dict[str, Any]) -> str:
    """A chip label: the record number (INV-00576, PLAT-151) or the name of a person or organisation."""
    head, _, rest = entity_id.partition(".")
    if head in ("person", "org"):
        return str((known.get(entity_id) or {}).get("title") or rest)
    if head == "gitlab":
        return "MR " + rest.rsplit("mr-", 1)[-1] if "mr-" in rest else rest
    if head == "confluence":
        return str((known.get(entity_id) or {}).get("title") or rest)[:40]
    return rest
