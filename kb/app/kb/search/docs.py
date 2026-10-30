"""Pure builders: journal rows -> OpenSearch documents and the texts that get embedded. No I/O here.

Document ids are `<tenant>:<object id>` because the indices are shared by tenants.
Embedded texts are immutable per object (chunk: title of the version that created it + section + text;
version: its own title, fields and body head), so the (encoder, sha256) cache always hits on a re-projection.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Iterable

from kb.text import norm

BODY_HEAD = 1500
DESC_FIELDS = 320
DESC_BODY = 320
EMBED_MAX = 2000


def doc_id(tenant_id: str, *parts: str) -> str:
    return ":".join((tenant_id, *parts))


def key_norm(text: str) -> str:
    """Normalisation of identifiers (FP-101, !42, #7, v2.4.0, flowpilot/app!42): like kb.text.norm but keeps the
    leading `!`/`#` that distinguish a merge request from an issue."""
    t = unicodedata.normalize("NFKC", text).casefold().strip()
    t = re.sub(r"\s+", " ", t)
    t = t.strip("«»\"“”„'`()[]{}<>")
    return t.rstrip(".,:;?!").lstrip(".,:;?")


def flatten_fields(fields: dict[str, Any] | None, limit: int = 4000) -> str:
    """Normalised fields as `name: value` lines (internal `@` fields skipped), bounded."""
    out: list[str] = []
    size = 0

    def val(v: Any, depth: int = 0) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, (int, float, str)):
            return str(v)
        if isinstance(v, list):
            return ", ".join(x for x in (val(i, depth + 1) for i in v[:50]) if x)
        if isinstance(v, dict) and depth < 3:
            return "; ".join(f"{k}: {s}" for k, x in list(v.items())[:30] if not str(k).startswith("@") and (s := val(x, depth + 1)))
        return str(v)

    for k, v in (fields or {}).items():
        if str(k).startswith("@"):
            continue
        s = val(v)
        if not s:
            continue
        line = f"{k}: {s}"
        out.append(line)
        size += len(line) + 1
        if size >= limit:
            break
    return "\n".join(out)[:limit]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def excerpt(text: str | None, n: int) -> str:
    if not text:
        return ""
    t = re.sub(r"\s+", " ", re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)).strip()
    if len(t) <= n:
        return t
    cut = t.rfind(" ", 0, n)
    return t[: cut if cut > n // 2 else n].rstrip(" ,;:") + "…"


# --------------------------------------------------------------------------------------------- chunks
def chunk_embed_text(chunk: dict, creating_title: str | None) -> str:
    parts = [creating_title or "", chunk.get("section") or "", chunk["text"]]
    return "\n".join(p for p in parts if p)[:EMBED_MAX]


def chunk_doc(tenant_id: str, ent: dict, chunk: dict, versions_by_seq: dict[int, dict], vector: list[float] | None,
              encoder: str | None) -> dict[str, Any]:
    """One chunk row spans versions [version_from_seq, version_to_seq] (NULL = still in the head)."""
    frm, to = chunk["version_from_seq"], chunk["version_to_seq"]
    last = to if to is not None else ent["head_seq"]
    span = [versions_by_seq[s] for s in range(frm, last + 1) if s in versions_by_seq and versions_by_seq[s]["op"] != "delete"]
    last_v = span[-1] if span else versions_by_seq.get(frm)
    effective = min((v["valid_from"] for v in span), default=None)
    doc = {
        "tenant_id": tenant_id,
        "chunk_id": chunk["chunk_id"],
        "entity_id": ent["entity_id"],
        "kind": ent["kind_id"],
        "source_id": ent.get("source_id"),
        "url": ent.get("url"),
        "acl": list(ent.get("acl") or []),
        "title": (last_v or {}).get("title") or ent.get("title"),
        "section": chunk.get("section"),
        "heading": chunk.get("section"),
        "text": chunk["text"],
        "ordinal": chunk["ordinal"],
        "char_start": chunk.get("char_start"),
        "char_end": chunk.get("char_end"),
        "version_from_seq": frm,
        "version_to_seq": to,
        "recorded_at": _iso(chunk["tx_from"]),
        "tx_to": _iso(chunk.get("tx_to")),
        "effective_from": _iso(effective),
        "is_current": to is None and ent.get("tombstone_tx") is None,
        "updated_tx": ent["updated_tx"],
    }
    if vector is not None:
        doc["embedding"] = vector
        doc["encoder"] = encoder
    return doc


# --------------------------------------------------------------------------------------------- versions
def version_text(v: dict) -> str:
    if v["op"] == "delete":
        return "deleted"
    parts = []
    changed = [f for f in v.get("changed_fields") or [] if not f.startswith("@")]
    if changed and v["op"] == "update":
        parts.append("changed: " + ", ".join(changed))
    f = flatten_fields(v.get("content"), 2000)
    if f:
        parts.append(f)
    if v.get("body"):
        parts.append(v["body"][:BODY_HEAD])
    return "\n".join(parts)


def version_embed_text(v: dict) -> str:
    return f"{v.get('title') or ''}\n{version_text(v)}"[:EMBED_MAX]


def version_doc(tenant_id: str, ent: dict, v: dict, vector: list[float] | None, encoder: str | None) -> dict[str, Any]:
    doc = {
        "tenant_id": tenant_id,
        "version_id": v["version_id"],
        "entity_id": ent["entity_id"],
        "seq": v["seq"],
        "op": v["op"],
        "kind": v["kind_id"],
        "source_id": ent.get("source_id"),
        "url": ent.get("url"),
        "acl": list(ent.get("acl") or []),
        "title": v.get("title"),
        "text": version_text(v),
        "changed_fields": [f for f in v.get("changed_fields") or [] if not f.startswith("@")],
        "actor_id": v.get("actor_id"),
        "recorded_at": _iso(v["recorded_at"]),
        "tx_to": _iso(v.get("superseded_at")),
        "effective_from": _iso(v["valid_from"]),
        "is_current": v.get("superseded_at") is None and ent.get("tombstone_tx") is None,
        "updated_tx": ent["updated_tx"],
    }
    if vector is not None:
        doc["embedding"] = vector
        doc["encoder"] = encoder
    return doc


# --------------------------------------------------------------------------------------------- entities
def entity_doc(tenant_id: str, ent: dict, head: dict, first: dict, keys: Iterable[str]) -> dict[str, Any]:
    text = "\n".join(p for p in (flatten_fields(head.get("content"), 2000), (head.get("body") or "")[:BODY_HEAD]) if p)
    return {
        "tenant_id": tenant_id,
        "entity_id": ent["entity_id"],
        "kind": ent["kind_id"],
        "source_id": ent.get("source_id"),
        "url": ent.get("url"),
        "acl": list(ent.get("acl") or []),
        "title": ent.get("title"),
        "text": text,
        "keys": sorted(set(keys)),
        "head_version_id": ent["head_version_id"],
        "head_seq": ent["head_seq"],
        "recorded_at": _iso(head["recorded_at"]),
        "effective_from": _iso(head["valid_from"]),
        "created_at": _iso(first["recorded_at"]),
        "updated_tx": ent["updated_tx"],
    }


# --------------------------------------------------------------------------------------------- descriptors
def alias_entries(rows: Iterable[dict]) -> list[dict[str, Any]]:
    """Alias rows (all origins, open and closed) -> nested descriptor entries, deduplicated."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        kind = r["alias_kind"]
        n = key_norm(r["alias"]) if kind == "key" else (r.get("alias_norm") or norm(r["alias"]))
        vf, vt = r.get("valid_from"), r.get("valid_to")
        current = bool(r.get("current"))
        k = (n, kind, current, vf, vt)
        if k in seen:
            continue
        seen[k] = {"text": r["alias"], "norm": n, "alias_kind": kind, "valid_from": _iso(vf), "valid_to": _iso(vt),
                   "current": current}
    # a current alias makes closed duplicates of the same name redundant
    cur = {(e["norm"], e["alias_kind"]) for e in seen.values() if e["current"]}
    out = [e for e in seen.values() if e["current"] or (e["norm"], e["alias_kind"]) not in cur]
    out.sort(key=lambda e: (not e["current"], e["alias_kind"], e["text"]))
    return out


def entity_description(kind: str, kind_label: str | None, version: dict | None) -> str:
    head = f"{kind_label} ({kind})" if kind_label and kind_label != kind else kind
    if not version:
        return head
    parts = [head]
    f = flatten_fields(version.get("content"), DESC_FIELDS).replace("\n", "; ")
    if f:
        parts.append(f[:DESC_FIELDS])
    b = excerpt(version.get("body"), DESC_BODY)
    if b:
        parts.append(b)
    return ". ".join(parts)


def entity_descriptor(tenant_id: str, ent: dict, kind_label: str | None, last_live: dict | None,
                      first: dict | None, deleted_at: datetime | None, alias_rows: Iterable[dict]) -> dict[str, Any]:
    aliases = alias_entries(alias_rows)
    title = ent.get("title") or ent["entity_id"]
    current_labels = [title] + [a["text"] for a in aliases if a["current"] and a["alias_kind"] != "key"]
    current_labels = list(dict.fromkeys(current_labels))
    deleted = ent.get("tombstone_tx") is not None
    keys = {key_norm(a["text"]) for a in aliases if a["current"] and a["alias_kind"] == "key"}
    keys.add(key_norm(ent["entity_id"]))
    return {
        "tenant_id": tenant_id,
        "doc_type": "entity",
        "ref_id": ent["entity_id"],
        "entity_id": ent["entity_id"],
        "kind": ent["kind_id"],
        "kind_label": " ".join(dict.fromkeys(x for x in (ent["kind_id"], kind_label) if x)),
        "source_id": ent.get("source_id"),
        "url": ent.get("url"),
        "acl": list(ent.get("acl") or []),
        "label": title,
        "labels": current_labels,
        "labels_norm": sorted({norm(x) for x in current_labels}),
        "keys_norm": sorted(keys),
        "aliases": aliases,
        "description": entity_description(ent["kind_id"], kind_label, last_live),
        "deleted": deleted,
        "exists_from": _iso(first["valid_from"]) if first else None,
        "deleted_at": _iso(deleted_at) if deleted else None,
        "updated_tx": ent["updated_tx"],
    }


def entity_descriptor_embed_text(doc: dict) -> str:
    others = [x for x in doc["labels"][1:]][:6]
    keys = [a["text"] for a in doc["aliases"] if a["alias_kind"] == "key" and a["current"]][:3]
    parts = [doc["label"], ", ".join(keys + others), doc["description"]]
    return ". ".join(p for p in parts if p)[:EMBED_MAX]


def predicate_descriptor(tenant_id: str, pred: dict, stats: dict[str, Any]) -> dict[str, Any]:
    """Schema-level descriptor of a predicate. `stats` (usage counts and endpoint-kind pairs aggregated over all
    instances) are stored unindexed and returned to admins only; nothing instance-level (titles) is stored."""
    pid = pred["predicate_id"]
    labels = [pred["label"]] + ([pred["inverse_label"]] if pred.get("inverse_label") else []) + list(pred.get("aliases") or [])
    labels = list(dict.fromkeys(x for x in labels if x))
    aliases = [{"text": a, "norm": norm(a), "alias_kind": "label", "valid_from": None, "valid_to": None, "current": True}
               for a in dict.fromkeys(pred.get("aliases") or [])]
    domain, rng = list(pred.get("domain_kinds") or []), list(pred.get("range_kinds") or [])
    desc_parts = [pred.get("description") or ""]
    if domain or rng:
        desc_parts.append(f"{'/'.join(domain) or '*'} → {'/'.join(rng) or '*'}")
    if pred.get("causal_kind"):
        desc_parts.append(f"causal: {pred['causal_kind']}")
    return {
        "tenant_id": tenant_id,
        "doc_type": "predicate",
        "ref_id": pid,
        "kind": "predicate",
        "acl": [],
        "label": pred["label"],
        "labels": labels,
        "labels_norm": sorted({norm(x) for x in labels} | {norm(pid.replace("_", " "))}),
        "keys_norm": sorted({key_norm(pid)}),
        "inverse_label": pred.get("inverse_label"),
        "aliases": aliases,
        "description": ". ".join(p for p in desc_parts if p),
        "causal_kind": pred.get("causal_kind"),
        "status": pred.get("status"),
        "canonical_predicate_id": pred.get("canonical_predicate_id"),
        "typical_sensitivity": list(pred.get("typical_sensitivity") or []),
        "domain_kinds": domain,
        "range_kinds": rng,
        "stats": stats,
    }


def predicate_embed_text(doc: dict) -> str:
    parts = [doc["label"], doc.get("inverse_label") or "", ", ".join(doc["labels"][1:]), doc["ref_id"].replace("_", " "),
             doc.get("description") or ""]
    return ". ".join(p for p in parts if p)[:EMBED_MAX]
