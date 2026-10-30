"""External market signals: Nemotron plans searches, Tavily runs them, Nemotron extracts
facts that cite the search results, and code verifies every citation.

The LLM never gets the last word on provenance: a fact survives only if its URL is one
of the results it was shown, its evidence quote is found in that result's text, and
every number in its statement appears in that text.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit

from variator.config import MissingCredentialsError
from variator.domain import Fact, FactOrigin
from variator.llm import LLMHTTPError, StructuredOutputError, TokenFactoryClient, user_message
from variator.llm.models import ROLE_MODEL
from variator.research.errors import TavilyAuthError, TavilyCreditsExhaustedError, TavilyError
from variator.research.models import (
    CitedFact,
    DroppedFact,
    ExtractResponse,
    ResearchBundle,
    ResearchQuery,
    SearchDepth,
    SearchResponse,
    SourceDocument,
    TimeRange,
)
from variator.research.prompts import (
    ExtractedFact,
    ExtractedFacts,
    PlannedQueries,
    extraction_prompt,
    query_planner_prompt,
)
from variator.research.tavily import TavilyClient

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 400  # Tavily rejects longer queries
# Configuration problems: retrying other queries cannot help, so they propagate.
_FATAL_ERRORS = (TavilyAuthError, MissingCredentialsError)
_LLM_ERRORS = (StructuredOutputError, LLMHTTPError)

_STOPWORDS = frozenset(
    "a an the of in on at to for by from with and or as is are was were be been its it this that "
    "than per into over year".split()
)
# Digits glued to a preceding letter (Q2, H1, FY2025) are labels, not quantities.
_NUMBER_RE = re.compile(r"(?<![A-Za-z\d.,])\d+(?:[.,]\d+)*")
_WORD_RE = re.compile(r"[^\w%]+")
_TRACKING_PARAMS = ("utm_", "gclid", "fbclid", "mc_")


def gather_external_facts(
    question: str,
    *,
    tf_client: TokenFactoryClient,
    tavily: TavilyClient,
    context_facts: Sequence[Fact] = (),
    max_queries: int = 3,
    results_per_query: int = 5,
    queries: Sequence[str | ResearchQuery] | None = None,
    search_depth: SearchDepth = "basic",
    time_range: TimeRange | None = None,
    extract_top: int = 0,
    model: str = ROLE_MODEL["extraction"],
    batch_size: int = 4,
    max_chars_per_source: int = 1500,
    max_facts: int = 25,
    max_workers: int = 4,
    today: date | None = None,
) -> ResearchBundle:
    """Find sourced external facts relevant to a decision question.

    1. ``model`` (Nemotron Lightning, reasoning off) proposes up to ``max_queries``
       focused searches, unless ``queries`` pins them (recommended for recorded demos,
       so offline cache keys never drift).
    2. Tavily runs them; results are deduplicated by URL across queries.
    3. Optionally ``extract_top`` best results are re-fetched with Tavily Extract
       (query-reranked chunks) for richer text than search snippets.
    4. The model extracts facts from the truncated texts in batches of ``batch_size``;
       code drops facts with foreign URLs, ungrounded evidence or invented numbers,
       then dedupes near-identical statements and keeps at most ``max_facts``.

    Non-fatal failures (one query rate-limited, an offline cache miss, credits running
    out, a batch the model could not parse) are listed in ``bundle.warnings``; a missing
    or invalid Tavily key raises.
    """
    if not question.strip():
        raise ValueError("question must not be empty")
    bundle = ResearchBundle(question=question.strip())

    if queries is not None:
        bundle.queries = _clean_queries(
            [q if isinstance(q, ResearchQuery) else ResearchQuery(query=q) for q in queries],
            limit=len(queries),
        )
    else:
        bundle.queries = _plan_with_fallback(
            bundle,
            tf_client=tf_client,
            context_facts=context_facts,
            max_queries=max_queries,
            today=today or date.today(),
            model=model,
        )

    def search(q: ResearchQuery) -> SearchResponse | None:
        return _search_one(
            q,
            tavily=tavily,
            search_depth=search_depth,
            time_range=time_range,
            max_results=results_per_query,
            warnings=bundle.warnings,
        )

    # Keep connector side effects in planner order. This makes recorded runs and
    # request traces reproducible; extraction can still use the parallel path below.
    responses = [search(query) for query in bundle.queries]
    found = [(q, r) for q, r in zip(bundle.queries, responses, strict=True) if r is not None]
    bundle.results = merge_results(found, max_chars=max_chars_per_source)
    billed: list[SearchResponse | ExtractResponse] = [r for _, r in found]

    if extract_top > 0 and bundle.results:
        extracted = _deepen(
            bundle, tavily=tavily, extract_top=extract_top, max_chars=2 * max_chars_per_source
        )
        if extracted is not None:
            billed.append(extracted)
    bundle.tavily_credits = sum(r.credits for r in billed)
    bundle.tavily_cache_hits = sum(r.from_cache for r in billed)

    if not bundle.results:
        bundle.warnings.append("no search results; no external facts gathered")
        return bundle

    batches = [bundle.results[i : i + batch_size] for i in range(0, len(bundle.results), batch_size)]

    def extract(batch: list[SourceDocument]) -> list[ExtractedFact]:
        return _extract_batch(
            bundle.question, batch, tf_client=tf_client, model=model, warnings=bundle.warnings
        )

    cited: list[CitedFact] = []
    for batch, raw_facts in zip(batches, _parallel_map(extract, batches, max_workers), strict=True):
        for raw in raw_facts:
            outcome = validate_fact(raw, batch)
            if isinstance(outcome, CitedFact):
                cited.append(outcome)
            else:
                bundle.dropped.append(outcome)

    bundle.cited_facts = _top_by_confidence(dedupe_facts(cited), max_facts)
    return bundle


def plan_queries(
    question: str,
    *,
    tf_client: TokenFactoryClient,
    context_facts: Sequence[Fact] = (),
    max_queries: int = 3,
    today: date | None = None,
    model: str = ROLE_MODEL["extraction"],
) -> list[ResearchQuery]:
    """Ask the model for focused search queries; cleaned, deduplicated and capped.

    Raises ``StructuredOutputError`` or ``LLMHTTPError`` if the model call fails.
    """
    prompt = query_planner_prompt(
        question, context_facts, max_queries=max_queries, today=today or date.today()
    )
    plan = tf_client.structured(
        model, [user_message(prompt)], PlannedQueries, reasoning=False, temperature=0.0, max_tokens=800
    )
    return _clean_queries(plan.queries, limit=max_queries)


def merge_results(
    found: Sequence[tuple[ResearchQuery, SearchResponse]], *, max_chars: int = 1500
) -> list[SourceDocument]:
    """Dedupe results by normalized URL across queries, merging distinct snippets.

    Source ids (S1, S2, ...) follow first appearance: query order, then Tavily rank.
    """
    docs: dict[str, SourceDocument] = {}
    for query, response in found:
        for result in response.results:
            key = normalize_url(result.url)
            if not key:
                continue
            snippet = " ".join(result.content.split())
            doc = docs.get(key)
            if doc is None:
                docs[key] = SourceDocument(
                    source_id=f"S{len(docs) + 1}",
                    url=result.url,
                    title=" ".join(result.title.split()),
                    content=snippet,
                    score=result.score,
                    published_date=result.published_date,
                    queries=[query.query],
                )
                continue
            if snippet and snippet not in doc.content:
                doc.content = f"{doc.content} [...] {snippet}" if doc.content else snippet
            doc.score = max(doc.score, result.score)
            doc.published_date = doc.published_date or result.published_date
            if query.query not in doc.queries:
                doc.queries.append(query.query)
    for doc in docs.values():
        doc.content = truncate(doc.content, max_chars)
    return list(docs.values())


def validate_fact(raw: ExtractedFact, sources: Sequence[SourceDocument]) -> CitedFact | DroppedFact:
    """Turn a model-proposed fact into a ``CitedFact``, or explain why it is dropped.

    ``sources`` are exactly the documents the model was shown for this fact.
    """
    statement = " ".join(raw.statement.split())
    cited_url = raw.source_url.strip()

    def drop(reason: str) -> DroppedFact:
        return DroppedFact(statement=statement, source_url=cited_url, reason=reason)

    if not statement:
        return drop("empty statement")
    doc = _find_source(cited_url, sources)
    if doc is None:
        return drop("source URL is not among the provided search results")
    source_text = " ".join(filter(None, [doc.title, doc.published_date, doc.content]))
    evidence = " ".join(raw.evidence.split())
    if not is_grounded(evidence, source_text):
        return drop("evidence quote not found in the source text")
    invented = numbers_in(statement) - numbers_in(source_text)
    if invented:
        return drop("numbers not in the source text: " + ", ".join(sorted(f"{n:g}" for n in invented)))

    value = raw.value if raw.value is not None and math.isfinite(raw.value) else None
    fact = Fact(
        statement=statement,
        value=value,
        unit=(raw.unit or "").strip() or None,
        period=(raw.period or "").strip() or None,
        origin=FactOrigin.WEB,
        source=doc.url,
        confidence=min(max(raw.confidence, 0.0), 1.0) if math.isfinite(raw.confidence) else 0.5,
    )
    return CitedFact(
        fact=fact,
        evidence=evidence,
        source_title=doc.title,
        published_date=doc.published_date,
        query=doc.queries[0] if doc.queries else None,
    )


def dedupe_facts(facts: Sequence[CitedFact], *, threshold: float = 0.8) -> list[CitedFact]:
    """Collapse near-identical statements, keeping the higher-confidence one.

    Two statements are duplicates when they contain the same numbers and their content
    words overlap by ``threshold`` (Jaccard). Different numbers never merge, so
    "rents rose 12%" and "rents rose 15%" both survive.
    """
    kept: list[CitedFact] = []
    for candidate in facts:
        for i, existing in enumerate(kept):
            if is_near_duplicate(candidate.fact.statement, existing.fact.statement, threshold):
                if candidate.fact.confidence > existing.fact.confidence:
                    kept[i] = candidate
                break
        else:
            kept.append(candidate)
    return kept


def is_near_duplicate(a: str, b: str, threshold: float = 0.8) -> bool:
    if numbers_in(a) != numbers_in(b):
        return False
    words_a, words_b = _content_words(a), _content_words(b)
    if not words_a or not words_b:
        return _normalize_text(a) == _normalize_text(b)
    return len(words_a & words_b) / len(words_a | words_b) >= threshold


def is_grounded(evidence: str, text: str, *, min_overlap: float = 0.8) -> bool:
    """True if ``evidence`` is (nearly) verbatim in ``text``.

    Exact match after normalizing case and punctuation, or at least ``min_overlap`` of
    the evidence's words (3+ chars) present in the text, to tolerate small edits and "...".
    """
    quote, haystack = _normalize_text(evidence), _normalize_text(text)
    if not quote:
        return False
    if quote in haystack:
        return True
    words = [w for w in quote.split() if len(w) >= 3]
    if not words:
        return False
    vocabulary = set(haystack.split())
    return sum(w in vocabulary for w in words) / len(words) >= min_overlap


def numbers_in(text: str) -> set[float]:
    """Numbers in text; "1,200" yields 1200 and 1.2 so both thousand and decimal commas match."""
    values: set[float] = set()
    for token in _NUMBER_RE.findall(text):
        values.add(float(token.replace(",", "")))
        if token.count(",") == 1 and "." not in token:
            values.add(float(token.replace(",", ".")))
    return values


def normalize_url(url: str) -> str:
    """Comparable form of a URL: no scheme, www, fragment, trailing slash or tracking params."""
    raw = url.strip().strip("<>()[]\"'")
    if not raw:
        return ""
    parts = urlsplit(raw if "://" in raw else "//" + raw)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if "." not in host:  # "S3" or junk is not a URL
        return ""
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    params = parse_qsl(parts.query, keep_blank_values=True)
    query = urlencode([(k, v) for k, v in params if not k.startswith(_TRACKING_PARAMS)])
    path = parts.path.rstrip("/")
    return f"{host}{port}{path}" + (f"?{query}" if query else "")


def truncate(text: str, limit: int) -> str:
    """Cut at a word boundary to keep prompts (and token cost) small."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip() + " ..."


def _plan_with_fallback(
    bundle: ResearchBundle,
    *,
    tf_client: TokenFactoryClient,
    context_facts: Sequence[Fact],
    max_queries: int,
    today: date,
    model: str,
) -> list[ResearchQuery]:
    try:
        planned = plan_queries(
            bundle.question,
            tf_client=tf_client,
            context_facts=context_facts,
            max_queries=max_queries,
            today=today,
            model=model,
        )
    except _LLM_ERRORS as exc:
        bundle.warnings.append(f"query planning failed ({type(exc).__name__}); searching the question itself")
        planned = []
    if planned:
        return planned
    return [ResearchQuery(query=truncate(bundle.question, MAX_QUERY_CHARS), purpose="the decision question")]


def _search_one(
    query: ResearchQuery,
    *,
    tavily: TavilyClient,
    search_depth: SearchDepth,
    time_range: TimeRange | None,
    max_results: int,
    warnings: list[str],
) -> SearchResponse | None:
    try:
        return tavily.search(
            query.query,
            search_depth=search_depth,
            topic=query.topic,
            max_results=max_results,
            time_range=time_range,
        )
    except _FATAL_ERRORS:
        raise
    except TavilyCreditsExhaustedError as exc:
        warnings.append(f"Tavily credits exhausted, skipped {query.query!r}: {exc.message}")
    except TavilyError as exc:
        warnings.append(f"search failed for {query.query!r}: {type(exc).__name__}: {exc.message}")
    return None


def _deepen(
    bundle: ResearchBundle, *, tavily: TavilyClient, extract_top: int, max_chars: int
) -> ExtractResponse | None:
    """Replace the snippets of the best results with query-reranked page chunks."""
    top = sorted(bundle.results, key=lambda d: d.score, reverse=True)[: min(extract_top, 20)]
    try:
        response = tavily.extract(
            [d.url for d in top], query=truncate(bundle.question, MAX_QUERY_CHARS), chunks_per_source=3
        )
    except _FATAL_ERRORS:
        raise
    except TavilyError as exc:
        bundle.warnings.append(f"extract failed: {type(exc).__name__}: {exc.message}")
        return None
    by_url = {normalize_url(d.url): d for d in top}
    for result in response.results:
        doc = by_url.get(normalize_url(result.url))
        text = " ".join((result.raw_content or "").split())
        if doc is not None and len(text) > len(doc.content):
            doc.content = truncate(text, max_chars)
    for failed in response.failed_results:
        log.info("Tavily extract failed for %s: %s", failed.url, failed.error)
    return response


def _extract_batch(
    question: str,
    batch: Sequence[SourceDocument],
    *,
    tf_client: TokenFactoryClient,
    model: str,
    warnings: list[str],
) -> list[ExtractedFact]:
    try:
        result = tf_client.structured(
            model,
            [user_message(extraction_prompt(question, batch))],
            ExtractedFacts,
            reasoning=False,
            temperature=0.0,
            max_tokens=2000,
        )
    except _LLM_ERRORS as exc:
        ids = ", ".join(d.source_id for d in batch)
        warnings.append(f"fact extraction failed for sources {ids}: {type(exc).__name__}")
        return []
    return result.facts


def _find_source(cited: str, sources: Sequence[SourceDocument]) -> SourceDocument | None:
    key = normalize_url(cited)
    for doc in sources:
        if key and normalize_url(doc.url) == key:
            return doc
    # Small models sometimes cite the block label ("S2") instead of copying the URL.
    label = cited.strip("[] ").upper()
    return next((doc for doc in sources if doc.source_id == label), None)


def _clean_queries(queries: Sequence[ResearchQuery], *, limit: int) -> list[ResearchQuery]:
    seen: set[str] = set()
    cleaned: list[ResearchQuery] = []
    for q in queries:
        text = truncate(" ".join(q.query.split()), MAX_QUERY_CHARS)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(q.model_copy(update={"query": text, "purpose": " ".join(q.purpose.split())}))
    return cleaned[: max(limit, 0)]


def _top_by_confidence(facts: list[CitedFact], limit: int) -> list[CitedFact]:
    if len(facts) <= limit:
        return facts
    best = sorted(range(len(facts)), key=lambda i: -facts[i].fact.confidence)[:limit]
    return [facts[i] for i in sorted(best)]


def _normalize_text(text: str) -> str:
    return " ".join(_WORD_RE.sub(" ", text.lower()).split())


def _content_words(text: str) -> set[str]:
    return {w for w in _normalize_text(text).split() if w not in _STOPWORDS}


def _parallel_map[T, R](fn: Callable[[T], R], items: Sequence[T], max_workers: int) -> list[R]:
    """Order-preserving map; threads only when it can help (Tavily and Token Factory are I/O)."""
    if max_workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(fn, items))
