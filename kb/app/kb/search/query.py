"""Pure query construction for hybrid search and grounding: filters, retriever bodies, RRF fusion, key tokens.

Every body built here starts from `base_filters` (tenant + ACL) so both retrievers score only documents the
principal may read: BM25 in a bool filter context, kNN through the engine's efficient filter.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from kb.acl import PrincipalCtx
from kb.search.docs import key_norm
from kb.text import norm

RRF_K = 60
VECTOR_MIN_COS = 0.3  # kNN candidates below this cosine are noise for the multilingual paraphrase encoder
# cosine noise floors per encoder family
MIN_COS_BY_PROVIDER = {"fastembed": VECTOR_MIN_COS, "hash": 0.0}


def min_cos() -> float:
    from kb.settings import get_settings

    from kb.search.embeddings import resolved_provider

    s = get_settings()
    return s.vector_min_cos if s.vector_min_cos is not None else MIN_COS_BY_PROVIDER.get(resolved_provider(), VECTOR_MIN_COS)


# BM25 weight in the fusion per encoder family: the local paraphrase model needs the full lexical channel to keep
# exact keys and numbers findable
LEXICAL_WEIGHT_BY_PROVIDER = {"fastembed": 1.0, "hash": 1.0}


def channel_weights() -> dict[str, float]:
    from kb.search.embeddings import resolved_provider
    from kb.settings import get_settings

    s = get_settings()
    lexical = s.search_lexical_weight if s.search_lexical_weight is not None else LEXICAL_WEIGHT_BY_PROVIDER.get(resolved_provider(), 1.0)
    return {"lexical": lexical, "vector": s.search_vector_weight}
TEXT_FIELDS = ["text", "text.en"]
TITLE_FIELDS = ["title^3", "title.en^3"]
HEADING_FIELDS = ["heading^2", "heading.en^2"]


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def base_filters(p: PrincipalCtx) -> list[dict[str, Any]]:
    return [{"term": {"tenant_id": p.tenant_id}}, *p.os_filter()]


def descriptor_acl(p: PrincipalCtx) -> list[dict[str, Any]]:
    """Entity descriptors follow the entity ACL; predicate descriptors are tenant-wide schema."""
    if p.is_admin:
        return []
    return [{"bool": {"should": [{"term": {"doc_type": "predicate"}}, *p.os_filter()], "minimum_should_match": 1}}]


def open_at(field_from: str, field_to: str, at: datetime) -> list[dict[str, Any]]:
    """[from, to) contains `at` (to missing = still open)."""
    a = iso(at)
    return [{"range": {field_from: {"lte": a}}},
            {"bool": {"should": [{"range": {field_to: {"gt": a}}}, {"bool": {"must_not": {"exists": {"field": field_to}}}}],
                      "minimum_should_match": 1}}]


def chunk_time_filters(valid_at: datetime | None, known_at: datetime | None, now: datetime) -> list[dict[str, Any]]:
    """Pre-filter of chunk docs for the (valid_at, known_at) slice; the exact version is checked afterwards.

    - neither: only chunks of current heads;
    - known_at only: chunks whose tx interval [recorded_at, tx_to) contains K (text edited away later is found);
    - valid_at: chunks recorded by K (default: the end of the data) that had started to be effective by T.
    """
    if valid_at is None and known_at is None:
        return [{"term": {"is_current": True}}]
    if valid_at is None:
        return open_at("recorded_at", "tx_to", known_at)  # type: ignore[arg-type]
    if known_at is None:  # as known at the end of the data: every chunk recorded so far
        return [{"range": {"effective_from": {"lte": iso(valid_at)}}}]
    return [{"range": {"recorded_at": {"lte": iso(known_at)}}}, {"range": {"effective_from": {"lte": iso(valid_at)}}}]


def lexical_chunks(q: str, filters: list[dict], size: int) -> dict[str, Any]:
    return {
        "size": size,
        "_source": {"excludes": ["embedding"]},
        "query": {"bool": {"filter": filters, "should": [
            {"multi_match": {"query": q, "fields": TEXT_FIELDS, "type": "most_fields"}},
            {"multi_match": {"query": q, "fields": TITLE_FIELDS, "type": "most_fields"}},
            {"multi_match": {"query": q, "fields": HEADING_FIELDS, "type": "most_fields"}},
        ], "minimum_should_match": 1}},
        "highlight": {"fields": {f: {} for f in TEXT_FIELDS}, "fragment_size": 220, "number_of_fragments": 1,
                      "pre_tags": ["<mark>"], "post_tags": ["</mark>"]},
    }


def knn_body(vector: list[float], filters: list[dict], size: int, field: str = "embedding") -> dict[str, Any]:
    return {
        "size": size,
        "_source": {"excludes": ["embedding"]},
        "query": {"knn": {field: {"vector": vector, "k": size, "filter": {"bool": {"filter": filters}}}}},
    }


def cosine_from_score(score: float) -> float:
    """OpenSearch cosinesimil score = (1 + cos) / 2."""
    return round(2.0 * score - 1.0, 4)


def rrf(channels: dict[str, list[str]], k: int = RRF_K, weights: dict[str, float] | None = None
        ) -> list[tuple[str, float, dict[str, int]]]:
    """Reciprocal rank fusion: score(d) = sum over channels of w_c / (k + rank). Ties: best single rank, then id."""
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for ch, ids in channels.items():
        w = (weights or {}).get(ch, 1.0)
        for r, d in enumerate(ids, 1):
            if ch in ranks.get(d, {}):
                continue  # a channel counts a document once (its best rank)
            scores[d] = scores.get(d, 0.0) + w / (k + r)
            ranks.setdefault(d, {})[ch] = r
    return sorted(((d, s, ranks[d]) for d, s in scores.items()), key=lambda x: (-x[1], min(x[2].values()), x[0]))


# --------------------------------------------------------------------------------------------- grounding
_TOKEN_STRIP = "«»\"“”„'`()[]{}<>,;:?"
_MR = re.compile(r"\b(?:mr|merge request)\s*!?\s*(\d+)\b", re.IGNORECASE)
_ISSUE = re.compile(r"\b(?:issue)\s*#?\s*(\d+)\b", re.IGNORECASE)


def key_tokens(text: str, limit: int = 24) -> dict[str, str]:
    """Identifier-like tokens of a free-text mention: key-normalised form -> the form as typed
    (FP-101, !42, #7, v2.4.0, ADR-014; "MR 42" -> !42, "issue 7" -> #7). The whole mention is a candidate too."""
    out: dict[str, str] = {}
    cands = [tok.strip(_TOKEN_STRIP) for tok in text.split()]
    cands += [f"!{m}" for m in _MR.findall(text)] + [f"#{m}" for m in _ISSUE.findall(text)] + [text.strip()]
    for c in cands:
        t = key_norm(c)
        if t and len(t) <= 80 and t not in out:
            out[t] = c.strip()
    return dict(list(out.items())[:limit])


def key_candidates(text: str, limit: int = 24) -> list[str]:
    return list(key_tokens(text, limit))


def _validity(at: datetime) -> list[dict[str, Any]]:
    a = iso(at)
    return [{"bool": {"should": [{"range": {"aliases.valid_from": {"lte": a}}},
                                 {"bool": {"must_not": {"exists": {"field": "aliases.valid_from"}}}}],
                      "minimum_should_match": 1}},
            {"bool": {"should": [{"range": {"aliases.valid_to": {"gt": a}}},
                                 {"bool": {"must_not": {"exists": {"field": "aliases.valid_to"}}}}],
                      "minimum_should_match": 1}}]


def _alias_clause(text: str, exact: list[str], name: str, flt: list[dict] | dict, boost: float) -> dict[str, Any]:
    must_not = []
    if isinstance(flt, dict):  # {"not": [...]} form: aliases NOT matching the filters
        must_not, flt = [{"bool": {"filter": flt["not"]}}], []
    return {"nested": {
        "path": "aliases",
        "score_mode": "max",
        "boost": boost,
        "query": {"bool": {
            "filter": flt,
            "must_not": must_not,
            "should": [
                {"constant_score": {"filter": {"terms": {"aliases.norm": exact}}, "boost": 10.0, "_name": "alias_exact"}},
                {"multi_match": {"query": text, "fields": ["aliases.text", "aliases.text.en"],
                                 "type": "most_fields", "_name": "alias_text"}},
            ],
            "minimum_should_match": 1}},
        "inner_hits": {"name": name, "size": 3, "_source": ["aliases.text", "aliases.alias_kind", "aliases.valid_from",
                                                            "aliases.valid_to", "aliases.current"]},
    }}


def ground_filters(p: PrincipalCtx, kind: str | None, entity_kinds: list[str] | None) -> list[dict[str, Any]]:
    f: list[dict[str, Any]] = [{"term": {"tenant_id": p.tenant_id}}, *descriptor_acl(p)]
    if kind in ("entity", "predicate"):
        f.append({"term": {"doc_type": kind}})
    if entity_kinds:
        if kind == "entity":
            f.append({"terms": {"kind": list(entity_kinds)}})
        elif kind is None:
            f.append({"bool": {"should": [{"term": {"doc_type": "predicate"}}, {"terms": {"kind": list(entity_kinds)}}],
                               "minimum_should_match": 1}})
    return f


def lexical_ground(text: str, filters: list[dict], size: int, as_of: datetime | None) -> dict[str, Any]:
    tokens = key_tokens(text)
    keys = list(tokens)
    full = norm(text)
    exact = list(dict.fromkeys(keys + ([full] if full else [])))
    should: list[dict[str, Any]] = [
        {"constant_score": {"filter": {"term": {"keys_norm": k}}, "boost": 50.0, "_name": f"key:{shown}"}}
        for k, shown in tokens.items()
    ]
    if full:
        should.append({"constant_score": {"filter": {"term": {"labels_norm": full}}, "boost": 20.0, "_name": "exact"}})
    should += [
        {"multi_match": {"query": text, "fields": ["label^3", "label.en^3"], "type": "most_fields",
                         "_name": "label"}},
        {"multi_match": {"query": text, "fields": ["labels^2", "labels.en^2"], "type": "most_fields",
                         "_name": "labels"}},
        {"multi_match": {"query": text, "fields": ["inverse_label^1.5", "inverse_label.en^1.5"],
                         "type": "most_fields", "_name": "inverse"}},
        # a word naming the kind ("experiment", "decision") is a type hint
        {"multi_match": {"query": text, "fields": ["kind_label", "kind_label.en"],
                         "type": "most_fields", "boost": 1.5, "_name": "kind"}},
        {"multi_match": {"query": text, "fields": ["description", "description.en"],
                         "type": "most_fields", "boost": 0.5, "_name": "description"}},
    ]
    if as_of is None:
        should += [_alias_clause(text, exact, "alias_current", [{"term": {"aliases.current": True}}], 1.0),
                   _alias_clause(text, exact, "alias_hist", [{"term": {"aliases.current": False}}], 0.4)]
    else:
        valid = _validity(as_of)
        should += [_alias_clause(text, exact, "alias_at", valid, 1.0),
                   _alias_clause(text, exact, "alias_other", {"not": valid}, 0.3)]
    return {
        "size": size,
        "_source": {"excludes": ["embedding", "aliases"]},
        "query": {"bool": {"filter": filters, "should": should, "minimum_should_match": 1}},
    }


LEX_SAT = 10.0  # BM25 score at which the lexical component of a grounding score reaches 0.5


def ground_score(lex_score: float | None, cos: float | None) -> float:
    """Calibrated fusion for grounding (RRF ranks would make a clear winner and its runner-up look tied):
    saturated BM25 (s / (s + LEX_SAT)) and the cosine above the encoder's noise floor, equally weighted."""
    lex = lex_score / (lex_score + LEX_SAT) if lex_score and lex_score > 0 else 0.0
    floor = min_cos()
    vec = max(0.0, ((cos or 0.0) - floor) / (1.0 - floor)) if cos is not None else 0.0
    return 0.5 * lex + 0.5 * vec


# bonus added to the fused score: an exact identifier or name match must beat a semantic near-miss
# (the kind hint is additive: a mention that names the kind, "the experiment with the checklist", prefers that kind)
BONUS = {"key": 1.0, "exact": 0.5, "alias_exact": 0.5, "alias_exact_hist": 0.25, "alias_exact_other": 0.15, "kind": 0.1}


def explain_ground(hit: dict[str, Any] | None, as_of: datetime | None) -> tuple[list[str], float]:
    """Explanations and the exact-match bonus of one lexical descriptor hit."""
    if hit is None:
        return [], 0.0
    src = hit.get("_source") or {}
    is_pred = src.get("doc_type") == "predicate"
    mq = hit.get("matched_queries") or []
    out: list[str] = []
    bonus = 0.0
    keys = [m.split(":", 1)[1] for m in mq if m.startswith("key:")]
    if keys:
        out.append("key " + ", ".join(keys))
        bonus = max(bonus, BONUS["key"])
    if "exact" in mq:
        out.append("exact label" if is_pred else "exact title")
        bonus = max(bonus, BONUS["exact"])
    if "label" in mq and "exact" not in mq:
        out.append("label" if is_pred else "title")
    if "labels" in mq and "label" not in mq and "exact" not in mq:
        out.append("alias")
    if "inverse" in mq:
        out.append("inverse label")
    kind_hint = "kind" in mq and not is_pred
    if kind_hint:
        out.append(f"kind {src.get('kind')}")
    for name, ih in (hit.get("inner_hits") or {}).items():
        for a in ih.get("hits", {}).get("hits", [])[:1]:
            s = a.get("_source") or {}
            amq = a.get("matched_queries") or []
            text = s.get("text")
            exact = "alias_exact" in amq
            if name in ("alias_current", "alias_at") and (s.get("current") or as_of is not None):
                if name == "alias_at" and not s.get("current"):
                    until = (s.get("valid_to") or "")[:10]
                    out.append(f"historical alias '{text}' (valid at as_of{', until ' + until if until else ''})")
                elif s.get("alias_kind") != "key" or not keys:
                    out.append(f"{'exact ' if exact else ''}alias '{text}'")
                if exact:
                    bonus = max(bonus, BONUS["alias_exact"])
            else:
                until = (s.get("valid_to") or "")[:10]
                label = f"historical alias '{text}'" + (f" until {until}" if until else "")
                if name == "alias_other" and s.get("current"):
                    label = f"alias '{text}' (not yet valid at as_of)"
                out.append(label)
                if exact:
                    bonus = max(bonus, BONUS["alias_exact_hist" if name == "alias_hist" else "alias_exact_other"])
    if "description" in mq and not out:
        out.append("description")
    return list(dict.fromkeys(out)), bonus + (BONUS["kind"] if kind_hint else 0.0)
