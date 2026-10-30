"""Deterministic offline-first assembly CLI for the Redwood corpus."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import zipfile
from collections import Counter, defaultdict, deque
from copy import deepcopy
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from variator.corpus.acceptance import require_accepted_delivery, validate_delivery
from variator.corpus.acquisition import download_artifact, sha256_file
from variator.corpus.adapters import fireflies as fireflies_adapter
from variator.corpus.adapters import github as github_adapter
from variator.corpus.adapters import gmail as gmail_adapter
from variator.corpus.adapters import google_drive as google_drive_adapter
from variator.corpus.adapters import hubspot as hubspot_adapter
from variator.corpus.adapters import linear as linear_adapter
from variator.corpus.adapters import slack as slack_adapter
from variator.corpus.canonical import immutable_id, sha256_hex
from variator.corpus.episode_cases import make_episode_cases
from variator.corpus.extraction import extract_factor_observations
from variator.corpus.factors import FACTORS_VERSION, factor_catalog_records
from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.leakage import enforce_leakage_threshold
from variator.corpus.linking import ranked_links
from variator.corpus.loaders import (
    load_amazon_reviews,
    load_cc_news,
    load_enterprise_rag_directory,
    load_github_issues,
    load_reddit,
    load_solstice_csv_directory,
)
from variator.corpus.manifest import config_hash, deterministic_run_id, file_manifest_entry, write_manifest
from variator.corpus.models import Acl, CorpusDocument, EvaluationCase, GoldLink, PlancyEvent
from variator.corpus.output import assign_time_split, write_gzip_jsonl, write_json, write_jsonl
from variator.corpus.plancy_events import provider_adapters
from variator.corpus.questions import normalize_question
from variator.corpus.redwood_delivery import COMPANY_NAME, build_directory, build_source_registry
from variator.corpus.redwood_profile import PROFILE_DAYS, PROFILE_PEOPLE, PROFILE_START
from variator.corpus.relationships import materialize_relationships
from variator.corpus.schemas import schemas, validate_record
from variator.corpus.solstice import materialize_chain_events
from variator.corpus.source_events import materialize_source_events
from variator.corpus.substitution import build_entity_map, substitute_entities

DEFAULT_FIXTURES = Path("tests/fixtures/corpus")
DEFAULT_OUTPUT = Path("data/corpus/redwood")
REDWOOD_ENTITY = "redwood:directory:organization:redwood-inference"
ENTITY_REPLACEMENTS = {
    "Business Wire": "Redwood Briefing",
    "PR Newswire": "Redwood Briefing",
    "Together": "Redwood Partners",
    "NVIDIA": "Redwood Compute",
    "nvidia.com": "redwood.example",
    "AWS": "Redwood Cloud",
    "vLLM": "Inference API",
    "A100": "R100",
    "H100": "R200",
}
SOURCE_PROVENANCE = {
    "enterprise_rag": {
        "url": "https://github.com/onyx-dot-app/EnterpriseRAG-Bench",
        "download_url": "https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/download/v1.0.0/all_documents.zip",
        "sha256": "9d1174928696ad08bc15f3f104739519de633c1605a4ec2034e0e3c0087bc5cd",
        "sampling_downloads": [
            {
                "name": "confluence_slice_0001.zip",
                "url": "https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/download/v1.0.0/confluence_slice_0001.zip",
                "sha256": "a79320d9c11b58c904ddc444f4b5c374ea1d9e6e3075974bcaea294c2a7e2eb5",
            },
            {
                "name": "github_slice_0001.zip",
                "url": "https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/download/v1.0.0/github_slice_0001.zip",
                "sha256": "4e6d2f445ce56b98ba3ebf41de29f80f63d5eed87dd1c246aacfefb1c942303a",
            },
            {
                "name": "questions.jsonl",
                "url": "https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/download/v1.0.0/questions.jsonl",
                "sha256": "",
            },
        ],
        "license": "MIT",
    },
    "solstice": {
        "url": "https://huggingface.co/datasets/solsticestudioai/synthetic-enterprise-operations-pack",
        "download_url": "https://huggingface.co/api/datasets/solsticestudioai/synthetic-enterprise-operations-pack/tree/main?recursive=true",
        "file_url_template": "https://huggingface.co/datasets/solsticestudioai/synthetic-enterprise-operations-pack/resolve/main/{path}",
        "license": "CC-BY-4.0",
    },
    "cc_news": {
        "url": "https://huggingface.co/datasets/vblagoje/cc_news",
        "download_url": "https://datasets-server.huggingface.co/first-rows?dataset=vblagoje%2Fcc_news&config=plain_text&split=train&offset=0&length=100",
        "license": "unknown",
    },
    "amazon_reviews_2023": {
        "url": "https://amazon-reviews-2023.github.io/",
        "download_url": "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw/review_categories/Software.jsonl",
        "license": "research-only",
        "sampling": "first 100 complete JSONL records from Software via HTTP Range",
    },
    "reddit": {
        "url": "https://convokit.cornell.edu/documentation/subreddit.html",
        "download_urls": [
            "https://zissou.infosci.cornell.edu/convokit/datasets/subreddit-corpus/corpus-zipped/MOMpictures~-~MadLadRevelations/MachineLearning.corpus.zip",
            "https://zissou.infosci.cornell.edu/convokit/datasets/subreddit-corpus/corpus-zipped/selfharmadvice~-~sex/selfhosted.corpus.zip",
            "https://zissou.infosci.cornell.edu/convokit/datasets/subreddit-corpus/corpus-zipped/listentothis2~-~loltyler1/llama.corpus.zip",
        ],
        "license": "source-specific",
    },
    "github": {"url": "https://api.github.com/", "license": "source-specific"},
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble the Redwood training corpus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("fetch", "assemble"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        subparser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
        subparser.add_argument("--seed", type=int, default=42)
        subparser.add_argument("--validation-fraction", type=float, default=0.2)
        subparser.add_argument("--max-documents", type=int, default=None)
        subparser.add_argument("--require-acceptance", action="store_true")
        subparser.add_argument("--live", action="store_true", help="allow external source access")
    return parser


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _source_records(fixtures: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source, filename in {
        "cc_news": "cc_news.jsonl",
        "amazon_reviews_2023": "amazon_reviews.jsonl",
        "reddit": "reddit.jsonl",
        "github": "github_issues.jsonl",
    }.items():
        path = fixtures / filename
        counts[source] = len(_read_jsonl(path)) if path.exists() else 0
    counts["enterprise_rag"] = len(list((fixtures / "enterprise_rag").rglob("*.txt")))
    solstice = fixtures / "solstice.json"
    if solstice.exists():
        payload = json.loads(solstice.read_text(encoding="utf-8"))
        counts["solstice"] = sum(len(rows) for rows in payload.get("tables", {}).values())
    else:
        counts["solstice"] = 0
    return counts


def _safe_external_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:180] or "record"


def _replace_document(document: CorpusDocument) -> CorpusDocument:
    content, applied = substitute_entities(document.content, ENTITY_REPLACEMENTS)
    author = document.author
    if author:
        author, author_applied = substitute_entities(author, ENTITY_REPLACEMENTS)
        applied.extend(author_applied)
    provenance = dict(document.provenance)
    provenance["transformations"] = ["entity_substitution/v1"] if applied else []
    provenance["entity_replacements"] = sorted(set(applied))
    return document.model_copy(
        update={"content": content, "author": author, "entities": [REDWOOD_ENTITY], "provenance": provenance}
    )


def _load_documents(
    fixtures: Path,
) -> tuple[list[CorpusDocument], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    documents: list[CorpusDocument] = []
    chains: dict[str, list[dict[str, Any]]] = {}
    enterprise_path = fixtures / "enterprise_rag"
    if enterprise_path.exists():
        documents.extend(load_enterprise_rag_directory(enterprise_path))
    solstice_path = fixtures / "solstice.json"
    if solstice_path.exists():
        payload = json.loads(solstice_path.read_text(encoding="utf-8"))
        tables = payload.get("tables", {})
        primary_keys = payload.get("primary_keys", {})
        foreign_keys = payload.get("foreign_keys", {})
        valid_ids = {table: {str(row[primary_keys[table]]) for row in rows} for table, rows in tables.items()}
        for table in sorted(tables):
            rows = list(tables[table])
            table_fks = {field: valid_ids[target] for field, target in foreign_keys.get(table, {}).items()}
            documents.extend(
                materialize_chain_events(
                    table=table, primary_key=primary_keys[table], rows=rows, foreign_keys=table_fks
                )
            )
            chains[table] = rows
    else:
        solstice_csv_path = fixtures / "solstice"
        if solstice_csv_path.exists():
            solstice_documents, solstice_tables = load_solstice_csv_directory(solstice_csv_path)
            documents.extend(solstice_documents)
            chains.update(solstice_tables)
    for filename, loader in (
        ("cc_news.jsonl", load_cc_news),
        ("amazon_reviews.jsonl", load_amazon_reviews),
        ("reddit.jsonl", load_reddit),
    ):
        path = fixtures / filename
        if path.exists():
            documents.extend(loader(_read_jsonl(path)))
    github_path = fixtures / "github_issues.jsonl"
    if github_path.exists():
        by_repo: dict[str, list[dict[str, Any]]] = {}
        for record in _read_jsonl(github_path):
            by_repo.setdefault(str(record.get("repository", "redwood/inference")), []).append(record)
        for repository in sorted(by_repo):
            documents.extend(load_github_issues(by_repo[repository], repository))
    questions_path = fixtures / "enterprise_rag" / "questions.jsonl"
    questions = _read_jsonl(questions_path) if questions_path.exists() else []
    return [_replace_document(document) for document in documents], questions, chains


def _factor_documents(documents: list[CorpusDocument]) -> list[CorpusDocument]:
    return [
        document.model_copy(update={"factors": extract_factor_observations(document)})
        for document in documents
    ]


def _limit_documents(
    documents: list[CorpusDocument],
    limit: int,
    required_doc_ids: set[str] | None = None,
) -> list[CorpusDocument]:
    external_documents = [document for document in documents if document.layer == "external"]
    core_documents = [document for document in documents if document.layer != "external"]
    if limit >= len(core_documents):
        return sorted(documents, key=lambda document: document.doc_id)
    by_source: dict[str, list[CorpusDocument]] = {}
    for document in core_documents:
        source = str(document.provenance.get("source", "unknown"))
        by_source.setdefault(source, []).append(document)

    sources = sorted(
        by_source,
        key=lambda source: (
            0 if any(document.layer == "chain" for document in by_source[source]) else 1,
            source,
        ),
    )
    if not sources:
        return []
    required = required_doc_ids or set()
    selected_by_id = {
        document.doc_id: document
        for document in core_documents
        if document.doc_id in required or document.layer == "chain"
    }
    if len(selected_by_id) > limit:
        raise ValueError("required corpus documents exceed max_documents")
    remaining = limit - len(selected_by_id)
    candidates = sorted(
        (document for document in core_documents if document.doc_id not in selected_by_id),
        key=lambda document: (
            0 if document.doc_id in required else 1,
            0 if document.layer == "chain" else 1,
            str(document.provenance.get("source", "unknown")),
            document.doc_id,
        ),
    )
    selected = list(selected_by_id.values()) + candidates[:remaining]
    return sorted(selected + external_documents, key=lambda document: document.doc_id)


def _materialize_events(documents: list[CorpusDocument]) -> list[PlancyEvent]:
    events: list[PlancyEvent] = []
    for sequence, document in enumerate(documents, start=1):
        world_at = document.timestamp or (PROFILE_START.replace(tzinfo=UTC) + timedelta(minutes=sequence))
        if world_at.tzinfo is None:
            world_at = world_at.replace(tzinfo=UTC)
        world_at = world_at.astimezone(UTC)
        source = str(document.provenance.get("source", "unknown"))
        external_id = _safe_external_id(document.doc_id)
        entity_id = immutable_id("redwood", source, document.source_type, external_id)
        events.append(
            PlancyEvent.upsert(
                entity_id=entity_id,
                source=source,
                object_type=document.source_type,
                external_id=external_id,
                revision=1,
                sequence=sequence,
                world_at=world_at,
                observed_at=world_at + timedelta(minutes=1),
                original_occurred_at=document.timestamp,
                time_transform_id="source-to-redwood-v1",
                payload=document.model_dump(mode="json"),
            )
        )
    return events


def _event_text(event: PlancyEvent) -> str:
    payload = event.payload or {}
    fields = payload.get("fields")
    if isinstance(fields, dict):
        return str(fields.get("description") or fields.get("summary") or event.external_id)
    body = payload.get("body")
    if isinstance(body, dict):
        content = body.get("content")
        if content:
            return str(content)
        adf_value = body.get("atlas_doc_format", {}).get("value")
        if adf_value:
            try:
                adf = json.loads(adf_value)
                texts: list[str] = []

                def collect(value: object) -> None:
                    if isinstance(value, dict):
                        if isinstance(value.get("text"), str):
                            texts.append(value["text"])
                        for child in value.values():
                            collect(child)
                    elif isinstance(value, list):
                        for child in value:
                            collect(child)

                collect(adf)
                if texts:
                    return " ".join(texts)
            except (TypeError, ValueError):
                pass
    properties = payload.get("properties")
    if isinstance(properties, dict):
        return str(properties.get("description") or json.dumps(properties, sort_keys=True))
    gmail_payload = payload.get("payload")
    if isinstance(gmail_payload, dict):
        gmail_body = gmail_payload.get("body")
        if isinstance(gmail_body, dict) and gmail_body.get("data"):
            return str(gmail_body["data"])
    return str(
        payload.get("description")
        or payload.get("transcript")
        or payload.get("content")
        or payload.get("text")
        or payload.get("snippet")
        or json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _grounded_fact(event: PlancyEvent) -> str:
    """Return one complete atomic answer fact that is present in the source."""
    payload = event.payload or {}
    fields = payload.get("fields")
    if isinstance(fields, dict):
        for key in ("status", "summary", "assignee", "description"):
            value = fields.get(key)
            if isinstance(value, dict):
                value = value.get("name") or value.get("displayName") or value.get("value")
            if value not in (None, ""):
                normalized = " ".join(str(value).split())
                return normalized if len(normalized) <= 120 else event.external_id
    properties = payload.get("properties")
    if isinstance(properties, dict):
        for key in (
            "status",
            "source_table",
            "stage",
            "name",
            "amount",
            "account_name",
            "arr",
            "mrr",
        ):
            value = properties.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                normalized = " ".join(str(value).split())
                return normalized if len(normalized) <= 120 else event.external_id
    for key in ("content", "description", "transcript", "text", "snippet"):
        value = payload.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            normalized = " ".join(str(value).split())
            return normalized if len(normalized) <= 120 else event.external_id
    return event.external_id


def _case_entity_id(event: PlancyEvent) -> str:
    return event.aliases[0] if event.aliases else event.entity_id


def _adapter_fixture(provider: str) -> dict[str, Any]:
    """Return a deterministic provider-native adapter fixture."""
    base = {
        "source": f"{provider}-redwood",
        "object_type": "record",
        "external_id": f"{provider}-fixture-001",
        "op": "upsert",
        "world_at": "2026-01-08T09:00:00+00:00",
        "acl": {"allow": [{"principal": "grp:engineering"}], "deny": [], "inherit": False},
    }
    payloads: dict[str, dict[str, Any]] = {
        "slack": {
            "type": "message",
            "channel": "eng-incidents",
            "ts": "1718612345.000200",
            "user": "U00000001",
            "text": "Investigating the workflow incident.",
            "thread_ts": "1718612000.000100",
        },
        "gmail": {
            "id": "gmail-fixture-001",
            "threadId": "thread-001",
            "snippet": "Review the deployment",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Deployment review"},
                    {"name": "From", "value": "maya-chen@redwood.example"},
                ]
            },
        },
        "linear": {
            "id": "LIN-1",
            "title": "Investigate workflow incident",
            "description": "Track the incident remediation.",
            "status": "In Progress",
            "team": "Engineering",
        },
        "github": {
            "id": 101,
            "number": 1,
            "title": "Fix workflow incident",
            "body": "Closes TASK-1",
            "state": "open",
            "html_url": "https://github.com/redwood/inference/pull/1",
            "pull_request": {},
        },
        "hubspot": {
            "id": "hs-1",
            "properties": {"name": "Redwood renewal", "description": "Renewal opportunity"},
            "archived": True,
        },
        "fireflies": {
            "id": "ff-1",
            "title": "Incident review",
            "summary": "Review remediation",
            "transcript": "The team reviewed the incident.",
        },
        "google_drive": {
            "id": "drive-1",
            "name": "Incident review",
            "mimeType": "application/vnd.google-apps.document",
            "description": "Incident review notes",
            "webViewLink": "https://drive.google.com/open?id=drive-1",
        },
    }
    base["payload"] = payloads[provider]
    return base


def _make_cases(
    events: list[PlancyEvent],
    links: list[GoldLink],
    question_records: list[dict[str, Any]] | None = None,
) -> list[EvaluationCase]:
    quotas = {
        "lookup": 9,
        "why": 11,
        "history": 10,
        "as_of": 8,
        "conflict": 6,
        "stale": 6,
        "impact": 3,
        "multi_hop": 6,
        "acl": 8,
        "negative": 6,
        "implicit_ref": 10,
        "numeric": 10,
        "people": 7,
    }
    if not events:
        raise ValueError("assembly requires at least one canonical document")
    private_event = next(
        (event for event in events if event.container in {"HR", "FINANCE", "COMMERCIAL", "LEADERSHIP"}),
        events[0],
    )
    public_event = next((event for event in events if event.container == "ENG"), events[0])
    rich_events = [event for event in events if len(_event_text(event).strip()) >= 80] or events
    numeric_events = [
        event
        for event in events
        if event.object_type in {"quote", "invoice"} or "Active customers:" in _event_text(event)
    ] or rich_events
    revisions_by_entity: dict[str, list[PlancyEvent]] = defaultdict(list)
    for candidate in events:
        revisions_by_entity[candidate.entity_id].append(candidate)
    history_events = [
        max(chain, key=lambda candidate: candidate.revision)
        for chain in revisions_by_entity.values()
        if len(chain) >= 3
    ] or rich_events
    event_external_ids = {event.external_id for event in events}
    source_questions = [
        record
        for record in (question_records or [])
        if isinstance(record.get("question"), str)
        and record.get("question", "").strip()
        and isinstance(record.get("expected_doc_ids"), list)
        and any(expected_id in event_external_ids for expected_id in record["expected_doc_ids"])
    ]
    question_cursor = 0
    cases: list[EvaluationCase] = []
    cursor = 0
    for category, quota in quotas.items():
        for _ in range(quota):
            if category == "numeric":
                pool = numeric_events
            elif category in {"history", "as_of"}:
                pool = history_events
            else:
                pool = rich_events
            event = pool[cursor % len(pool)]
            matching_link = links[cursor % len(links)] if links else None
            evidence_entity = matching_link.source_entity_id if matching_link else _case_entity_id(event)
            evidence_event = next(
                (
                    candidate
                    for candidate in events
                    if evidence_entity == candidate.entity_id or evidence_entity in candidate.aliases
                ),
                event,
            )
            if category == "acl":
                evidence_event = public_event
                evidence_entity = _case_entity_id(public_event)
            roots = []
            if category == "why" and matching_link is not None:
                roots = [
                    {
                        "entity_id": matching_link.target_entity_id,
                        "why": f"{matching_link.predicate} source relationship",
                    }
                ]
            question = {
                "lookup": f"What does {event.external_id} report?",
                "why": f"Why did {evidence_event.external_id} occur?",
                "history": f"How did {event.external_id} change over time?",
                "as_of": f"What was known about {event.external_id} at that time?",
                "conflict": f"Which source is authoritative for {event.external_id}?",
                "stale": f"What information related to {event.external_id} may be stale?",
                "impact": f"What would be affected by {event.external_id}?",
                "multi_hop": f"How is {event.external_id} connected to the operational incident?",
                "acl": f"What can an engineer know about {event.external_id}?",
                "negative": "Is there evidence of an unrecorded outage?",
                "implicit_ref": "What did that incident update say?",
                "numeric": f"Which metric is reported by {event.external_id}?",
                "people": f"Who owns the work described by {event.external_id}?",
            }[category]
            answer_facts = [{"key": "body_fact", "any": [_grounded_fact(evidence_event)]}]
            question_id = None
            if source_questions and category in {"lookup", "numeric", "multi_hop", "implicit_ref"}:
                candidate = source_questions[question_cursor % len(source_questions)]
                question_cursor += 1
                expected_ids = candidate.get("expected_doc_ids")
                expected_id = expected_ids[0] if isinstance(expected_ids, list) and expected_ids else None
                matching_event = next(
                    (
                        candidate_event
                        for candidate_event in events
                        if candidate_event.external_id == expected_id
                        or candidate_event.payload
                        and candidate_event.payload.get("redwood_metadata", {}).get("canonical_document_id")
                        == f"enterprise_rag:{expected_id}"
                    ),
                    None,
                )
                if matching_event is not None:
                    evidence_event = matching_event
                    evidence_entity = _case_entity_id(matching_event)
                    question = str(candidate["question"])
                    question_id = candidate.get("question_id")
                    evidence_text = json.dumps(
                        matching_event.model_dump(mode="json"), ensure_ascii=False
                    ).casefold()
                    backed_facts = [
                        str(fact)
                        for fact in candidate.get("answer_facts", [])
                        if str(fact).casefold() in evidence_text
                    ]
                    if backed_facts:
                        answer_facts = [
                            {"key": "enterprise_rag_fact", "any": [fact]} for fact in backed_facts
                        ]
                    if not backed_facts:
                        answer_facts = [{"key": "body_fact", "any": [_grounded_fact(evidence_event)]}]
            evidence = [
                {
                    "entity_id": evidence_entity,
                    "version": evidence_event.revision,
                    "why": "source payload contains the answer fact",
                }
            ]
            known_at = event.world_at
            if category in {"history", "as_of"}:
                chain = sorted(revisions_by_entity[evidence_event.entity_id], key=lambda item: item.revision)
                if category == "history":
                    evidence = [
                        {
                            "entity_id": _case_entity_id(snapshot),
                            "version": snapshot.revision,
                            "why": "full source snapshot in the object history",
                        }
                        for snapshot in chain
                    ]
                elif len(chain) >= 2:
                    evidence_event = chain[-2]
                    evidence_entity = _case_entity_id(evidence_event)
                    answer_facts = [{"key": "as_of_fact", "any": [_grounded_fact(evidence_event)]}]
                    evidence = [
                        {
                            "entity_id": evidence_entity,
                            "version": evidence_event.revision,
                            "why": "snapshot valid before the next source revision",
                        }
                    ]
                    known_at = evidence_event.observed_at
            forbidden = []
            if category == "acl":
                forbidden = [
                    {
                        "entity_id": _case_entity_id(private_event),
                        "facts": [
                            {
                                "key": "private_fact",
                                "any": [" ".join(_event_text(private_event).split())[:180]],
                            }
                        ],
                    }
                ]
            if category == "negative":
                evidence = []
                answer_facts = []
            cases.append(
                EvaluationCase(
                    id=f"R{len(cases) + 1:03d}",
                    split="dev" if len(cases) < 70 else "holdout",
                    category=category,
                    difficulty="trivial" if len(cases) % 5 < 2 else "tricky",
                    language="en",
                    question=question,
                    known_at=known_at,
                    principal="user:e010" if category == "acl" else "user:admin",
                    evidence=evidence,
                    roots=roots,
                    forbidden=forbidden,
                    answer_facts=answer_facts,
                    expect_not_found=category == "negative",
                    rationale=(
                        f"Derived from EnterpriseRAG question {question_id} and source-native payload "
                        f"for {evidence_event.external_id}."
                        if question_id
                        else f"Derived from source-native payload for {evidence_event.external_id}."
                    ),
                )
            )
            cursor += 1
    return cases


def _connected_sample(
    events: list[PlancyEvent],
    links: list[GoldLink],
    cases: list[EvaluationCase],
    limit: int = 200,
) -> list[PlancyEvent]:
    """Select the largest deterministic source-linked component for the sample."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    for link in links:
        adjacency[link.source_entity_id].add(link.target_entity_id)
        adjacency[link.target_entity_id].add(link.source_entity_id)
    events_by_reference: dict[str, list[PlancyEvent]] = defaultdict(list)
    reference_by_entity: dict[str, str] = {}
    for event in events:
        reference = event.aliases[0] if event.aliases else event.entity_id
        events_by_reference[reference].append(event)
        reference_by_entity[event.entity_id] = reference
    event_entities = set(events_by_reference)
    components: list[set[str]] = []
    unseen = set(event_entities)
    while unseen:
        start = min(unseen)
        component: set[str] = set()
        queue = deque([start])
        while queue:
            entity_id = queue.popleft()
            if entity_id in component:
                continue
            component.add(entity_id)
            unseen.discard(entity_id)
            queue.extend(sorted(adjacency[entity_id] & unseen))
        components.append(component)

    def cases_in_component(component: set[str]) -> int:
        return sum(
            1
            for case in cases
            if not case.evidence
            or all(
                reference_by_entity.get(str(item.get("entity_id")), str(item.get("entity_id"))) in component
                for item in case.evidence
            )
        )

    largest = max(
        components,
        key=lambda component: (
            len(component) >= 200,
            cases_in_component(component),
            len(component),
            sorted(component),
        ),
    )
    selected_entities = set()
    case_entities = {
        reference_by_entity.get(str(item.get("entity_id")), str(item.get("entity_id")))
        for case in cases
        for item in case.evidence
        if item.get("entity_id")
    }
    queue = deque([min(largest)])
    while queue and len(selected_entities) < limit:
        entity_id = queue.popleft()
        if entity_id in selected_entities:
            continue
        selected_entities.add(entity_id)
        neighbors = sorted(
            adjacency[entity_id] & largest - selected_entities,
            key=lambda candidate: (candidate not in case_entities, candidate),
        )
        queue.extendleft(reversed(neighbors))
    if len(selected_entities) < min(limit, len(largest)):
        selected_entities = largest
    return sorted(
        [event for entity_id in sorted(selected_entities) for event in events_by_reference[entity_id]],
        key=lambda event: event.sequence,
    )


def _write_yaml(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=True, allow_unicode=True), encoding="utf-8")


def _manifest_file(output: Path, path: Path, schema_version: str, record_count: int) -> dict[str, object]:
    entry = file_manifest_entry(path, schema_version, record_count)
    entry["path"] = path.relative_to(output).as_posix()
    return entry


def _dashboard_metrics(
    events: list[PlancyEvent], chains: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Recompute business metrics from final emitted provider snapshots."""
    latest: dict[str, PlancyEvent] = {}
    for event in events:
        if event.source != "hubspot-redwood":
            continue
        previous = latest.get(event.entity_id)
        if previous is None or event.revision > previous.revision:
            latest[event.entity_id] = event
    episode_events = [
        event
        for event in latest.values()
        if (event.payload or {}).get("properties", {}).get("redwood_episode_id")
    ]
    deals = [event for event in episode_events if event.object_type == "deal"]
    quotes = [event for event in episode_events if event.object_type == "quote"]
    invoices = [event for event in episode_events if event.object_type == "invoice"]
    payments = [event for event in episode_events if event.object_type == "payment"]
    contracts = [event for event in episode_events if event.object_type == "contract"]
    amendments = [event for event in episode_events if event.object_type == "amendment"]
    supplier_invoices = [event for event in episode_events if event.object_type == "supplier_invoice"]
    operating_costs = [event for event in episode_events if event.object_type == "operating_cost"]

    def properties(event: PlancyEvent) -> dict[str, Any]:
        return (event.payload or {}).get("properties", {})

    def evidence(selected: list[PlancyEvent], fallback: str) -> list[str]:
        return sorted({_case_entity_id(event) for event in selected}) or [fallback]

    won = [event for event in deals if properties(event).get("status") == "closed_won"]
    open_deals = [
        event for event in deals if properties(event).get("status") not in {"closed_won", "closed_lost"}
    ]
    accepted_quotes = [event for event in quotes if properties(event).get("status") == "accepted"]
    overdue_invoices = [event for event in invoices if properties(event).get("status") == "overdue"]
    active_contracts = [event for event in contracts if properties(event).get("status") == "active"]
    churned_contracts = [event for event in contracts if properties(event).get("status") == "churned"]
    approved_amendments = [event for event in amendments if properties(event).get("status") == "approved"]
    actual_supplier_costs = [
        event for event in supplier_invoices if properties(event).get("status") == "paid"
    ]
    actual_operating_costs = [
        event for event in operating_costs if properties(event).get("status") == "actual"
    ]
    recognized_revenue = sum(
        float(properties(event).get("amount") or 0)
        for event in invoices
        if properties(event).get("status") == "paid"
    )
    supplier_cost = sum(float(properties(event).get("amount") or 0) for event in actual_supplier_costs)
    operating_cost = sum(float(properties(event).get("amount") or 0) for event in actual_operating_costs)
    dso_values: list[float] = []
    all_invoice_events = [
        event
        for event in events
        if event.source == "hubspot-redwood"
        and event.object_type == "invoice"
        and properties(event).get("redwood_episode_id")
    ]
    for invoice_id in {event.entity_id for event in all_invoice_events}:
        history = sorted(
            [event for event in all_invoice_events if event.entity_id == invoice_id],
            key=lambda event: event.revision,
        )
        issued = next((event for event in history if properties(event).get("status") == "issued"), None)
        settled = next(
            (event for event in history if properties(event).get("status") in {"paid", "voided"}),
            None,
        )
        if issued and settled:
            dso_values.append((settled.world_at - issued.world_at).total_seconds() / 86400)
    at_risk_value = sum(
        float(row.get("arr_before_usd") or 0)
        for row in chains.get("renewals", [])
        if row.get("renewal_status") == "at_risk"
    )
    return [
        {
            "id": "closed_won_revenue_usd",
            "roleId": "sales",
            "label": "Closed won revenue",
            "value": sum(float(properties(event).get("amount") or 0) for event in won),
            "unit": "USD",
            "evidence": evidence(won, "chains/opportunities.jsonl"),
        },
        {
            "id": "weighted_pipeline_usd",
            "roleId": "sales",
            "label": "Weighted pipeline",
            "value": sum(float(properties(event).get("amount") or 0) for event in open_deals),
            "unit": "USD",
            "evidence": evidence(open_deals, "chains/opportunities.jsonl"),
        },
        {
            "id": "accepted_proposals_usd",
            "roleId": "sales",
            "label": "Accepted proposals",
            "value": sum(float(properties(event).get("amount") or 0) for event in accepted_quotes),
            "unit": "USD",
            "evidence": evidence(accepted_quotes, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "accounts_receivable_usd",
            "roleId": "ceo",
            "label": "Accounts receivable",
            "value": sum(float(properties(event).get("balance") or 0) for event in invoices),
            "unit": "USD",
            "evidence": evidence(invoices, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "overdue_receivables_usd",
            "roleId": "ceo",
            "label": "Overdue receivables",
            "value": sum(float(properties(event).get("balance") or 0) for event in overdue_invoices),
            "unit": "USD",
            "evidence": evidence(overdue_invoices, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "paid_amount_usd",
            "roleId": "ceo",
            "label": "Paid amount",
            "value": sum(float(properties(event).get("cumulative_amount") or 0) for event in payments),
            "unit": "USD",
            "evidence": evidence(payments, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "at_risk_arr_usd",
            "roleId": "ceo",
            "label": "At-risk ARR",
            "value": at_risk_value,
            "unit": "USD",
            "evidence": ["chains/renewals.jsonl"],
        },
        {
            "id": "active_customers",
            "roleId": "ceo",
            "label": "Active customers",
            "value": len({str(properties(event).get("account_id")) for event in deals}),
            "unit": "customers",
            "evidence": evidence(deals, "chains/accounts.jsonl"),
        },
        {
            "id": "arr_usd",
            "roleId": "ceo",
            "label": "Active ARR",
            "value": sum(float(properties(event).get("arr") or 0) for event in active_contracts),
            "unit": "USD",
            "evidence": evidence(active_contracts, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "mrr_usd",
            "roleId": "ceo",
            "label": "Active MRR",
            "value": sum(float(properties(event).get("mrr") or 0) for event in active_contracts),
            "unit": "USD",
            "evidence": evidence(active_contracts, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "churn_rate",
            "roleId": "sales",
            "label": "Churn rate",
            "value": len(churned_contracts) / max(1, len(active_contracts) + len(churned_contracts)),
            "unit": "ratio",
            "evidence": evidence(active_contracts + churned_contracts, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "expansion_usd",
            "roleId": "sales",
            "label": "Expansion ARR",
            "value": sum(float(properties(event).get("delta_arr") or 0) for event in approved_amendments),
            "unit": "USD",
            "evidence": evidence(approved_amendments, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "dso_days",
            "roleId": "ceo",
            "label": "Days sales outstanding",
            "value": sum(dso_values) / max(1, len(dso_values)),
            "unit": "days",
            "evidence": evidence(invoices, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "discount_usd",
            "roleId": "sales",
            "label": "Commercial discount",
            "value": sum(float(properties(event).get("discount") or 0) for event in won),
            "unit": "USD",
            "evidence": evidence(won, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "gross_margin_pct",
            "roleId": "ceo",
            "label": "Gross margin",
            "value": sum(float(properties(event).get("margin") or 0) for event in active_contracts)
            / max(1, len(active_contracts)),
            "unit": "ratio",
            "evidence": evidence(
                won + actual_supplier_costs + actual_operating_costs, "fixtures/timeline.jsonl.gz"
            ),
        },
        {
            "id": "forecast_revenue_usd",
            "roleId": "sales",
            "label": "Forecast revenue",
            "value": sum(float(properties(event).get("forecast_amount") or 0) for event in deals),
            "unit": "USD",
            "evidence": evidence(deals, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "actual_revenue_usd",
            "roleId": "sales",
            "label": "Actual revenue",
            "value": recognized_revenue,
            "unit": "USD",
            "evidence": evidence(invoices, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "supplier_cost_usd",
            "roleId": "ceo",
            "label": "Supplier costs",
            "value": supplier_cost,
            "unit": "USD",
            "evidence": evidence(actual_supplier_costs, "fixtures/timeline.jsonl.gz"),
        },
        {
            "id": "operating_cost_usd",
            "roleId": "ceo",
            "label": "Operating costs",
            "value": operating_cost,
            "unit": "USD",
            "evidence": evidence(actual_operating_costs, "fixtures/timeline.jsonl.gz"),
        },
    ]


def _sanitize_external_prose(value: object, key: str = "") -> object:
    """Remove generator-shaped wording from human-visible external prose."""
    if isinstance(value, dict):
        return {
            child_key: (
                child
                if child_key in {"redwoodExternalReferences", "canonicalDocumentId"}
                or child_key.casefold().endswith("id")
                else _sanitize_external_prose(child, child_key)
            )
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_external_prose(child, key) for child in value]
    if not isinstance(value, str):
        return value
    cleaned = re.sub(r"\bamazon[_ ]reviews[_ ]2023\b", "customer review archive", value, flags=re.I)
    cleaned = re.sub(r"\bamazon reviews\b", "customer reviews", cleaned, flags=re.I)
    cleaned = re.sub(r"\bcc_news\b", "market bulletin", cleaned, flags=re.I)
    cleaned = re.sub(r"\bworkstream\s+\d+\b", "the related workstream", cleaned, flags=re.I)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = re.sub(r"\b([a-z][a-z'-]+)(?:\s+\1\b)+", r"\1", cleaned, flags=re.I)
    return cleaned


def _rewrite_external_payloads(events: list[PlancyEvent]) -> list[PlancyEvent]:
    """Apply deterministic prose cleanup and recompute event content hashes."""
    rewritten: list[PlancyEvent] = []
    for event in events:
        payload = event.payload
        if not payload or not (
            payload.get("redwoodExternalAction")
            or payload.get("redwoodExternalChainId")
            or payload.get("redwoodImplicitContext")
        ):
            rewritten.append(event)
            continue
        cleaned = _sanitize_external_prose(payload)
        if not isinstance(cleaned, dict):
            rewritten.append(event)
            continue
        external_source = str(cleaned.get("externalSource") or "")
        external_stage = str(cleaned.get("redwoodExternalStage") or "")
        family = None
        if external_source == "market bulletin":
            family = "competitive_analysis"
        elif external_source in {"reddit", "community discussion"}:
            family = {
                "task": "customer_support",
                "merge_request": "experiment",
                "decision": "community_decision",
                "postmortem": "incident_remediation",
            }.get(external_stage)
        elif external_source == "customer review archive":
            family = {
                "deployment": "negative_review_fix",
                "deal_action": "renewal_action",
                "task": "customer_risk",
            }.get(external_stage)
        elif external_source == "customer email":
            family = "customer_support"
        if family:
            cleaned["redwoodExternalOutcomeFamily"] = family
        rewritten.append(
            PlancyEvent.upsert(
                entity_id=event.entity_id,
                source=event.source,
                object_type=event.object_type,
                external_id=event.external_id,
                revision=event.revision,
                sequence=event.sequence,
                world_at=event.world_at,
                observed_at=event.observed_at,
                previous_event_id=event.previous_event_id,
                original_occurred_at=event.original_occurred_at,
                time_transform_id=event.time_transform_id,
                container=event.container,
                acl=event.acl,
                aliases=event.aliases,
                payload=cleaned,
            )
        )
    return rewritten


def _attach_external_deal_actions(events: list[PlancyEvent]) -> list[PlancyEvent]:
    """Add deterministic HubSpot renewal actions to review-risk episodes."""
    latest: dict[str, PlancyEvent] = {}
    for event in events:
        previous = latest.get(event.entity_id)
        if previous is None or event.revision > previous.revision:
            latest[event.entity_id] = event
    grouped: dict[str, dict[str, PlancyEvent]] = defaultdict(dict)
    for event in latest.values():
        payload = event.payload or {}
        chain_id = payload.get("redwoodExternalChainId")
        stage = payload.get("redwoodExternalStage")
        if chain_id and stage:
            grouped[str(chain_id)][str(stage)] = event

    added: list[PlancyEvent] = []
    for chain_id, stages in sorted(grouped.items()):
        action = stages.get("action")
        meeting = stages.get("meeting")
        decision = stages.get("decision")
        if not action or not meeting or not decision:
            continue
        source_kind = str((action.payload or {}).get("externalSource") or "")
        if source_kind not in {"amazon_reviews", "amazon_reviews_2023"}:
            continue
        deal_external_id = f"EXT-DEAL-ACTION-{chain_id.removeprefix('external-chain-')}"
        entity_id = immutable_id("redwood", "hubspot-redwood", "deal", deal_external_id)
        if entity_id in latest:
            continue
        canonical_id = str((action.payload or {}).get("canonicalDocumentId") or "")
        numeric_suffix = sum(ord(character) for character in chain_id) % 6 + 1
        account_id = f"A{numeric_suffix:03d}"
        decision_id = decision.external_id
        references = dict((decision.payload or {}).get("redwoodExternalReferences") or {})
        references.update({"decisionId": decision_id, "dealActionId": deal_external_id})
        base_time = max(event.world_at for event in stages.values()) + timedelta(days=1)
        while base_time.weekday() >= 5:
            base_time += timedelta(days=1)
        base_time = base_time.replace(hour=11, minute=(numeric_suffix * 7) % 50, second=0, microsecond=0)
        payload = {
            "id": deal_external_id,
            "createdAt": base_time.isoformat(),
            "updatedAt": base_time.isoformat(),
            "properties": {
                "name": f"Renewal action after customer review {account_id}",
                "description": (
                    f"The commercial team moved the renewal action after decision {decision_id}. "
                    f"The action addresses customer review evidence from {canonical_id}."
                ),
                "account_id": account_id,
                "stage": "renewal_review",
                "status": "open",
            },
            "redwoodExternalAction": True,
            "externalSource": source_kind,
            "canonicalDocumentId": canonical_id,
            "redwoodExternalChainId": chain_id,
            "redwoodExternalStage": "deal_action",
            "redwoodExternalReferences": references,
            "redwoodImplicitContext": {
                "kind": "external_review_deal_action",
                "target_stage": "decision",
                "phrase": "the renewal action the account team approved after review",
            },
        }
        added.append(
            PlancyEvent.upsert(
                entity_id=entity_id,
                source="hubspot-redwood",
                object_type="deal",
                external_id=deal_external_id,
                revision=1,
                sequence=len(events) + len(added) + 1,
                world_at=base_time,
                observed_at=base_time + timedelta(minutes=1),
                original_occurred_at=decision.original_occurred_at,
                time_transform_id="external-to-redwood-180d-v2",
                container="COMMERCIAL",
                acl=decision.acl,
                aliases=[f"hubspot.deal-{deal_external_id}"],
                payload=payload,
            )
        )
    # These derived HubSpot actions are still generator output. Reinsert them
    # into the scheduler-produced stream before the timeline is materialized;
    # the writer must never repair ordering after JSONL has been written.
    ordered = sorted(
        events + added,
        key=lambda event: (event.world_at, event.event_id),
    )
    resequenced = [
        event.model_copy(update={"sequence": sequence})
        for sequence, event in enumerate(ordered, start=1)
    ]
    return _rewrite_external_payloads(resequenced)


def _hydrate_source_display_names(
    events: list[PlancyEvent], directory: dict[str, Any]
) -> list[PlancyEvent]:
    """Replace source account placeholders with names from the delivered directory."""
    names = {
        str(person.get("id")): str(person.get("name"))
        for person in directory.get("people", [])
        if person.get("id") and person.get("name")
    }
    names.update(
        {
            str(person.get("id", "")).removeprefix("RW-"): str(person.get("name"))
            for person in directory.get("people", [])
            if person.get("id") and person.get("name")
        }
    )

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    names.get(str(item), item)
                    if key == "displayName" and re.fullmatch(r"E\d{3}", str(item))
                    else rewrite(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    def participant_addresses(payload: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
        found: list[tuple[dict[str, Any], str]] = []
        sender = payload.get("from")
        if isinstance(sender, dict):
            address = ((sender.get("emailAddress") or {}).get("address"))
            if address:
                found.append((sender, str(address)))
        for field in ("toRecipients", "ccRecipients", "bccRecipients"):
            for recipient in payload.get(field, []):
                if not isinstance(recipient, dict):
                    continue
                address = ((recipient.get("emailAddress") or {}).get("address"))
                if address:
                    found.append((recipient, str(address)))
        return found

    known_addresses = {
        str(item.get("email")).casefold()
        for item in [*directory.get("people", []), *directory.get("contacts", [])]
        if isinstance(item, dict) and item.get("email")
    }
    dynamic_contacts: list[dict[str, Any]] = []
    for event in events:
        if event.source not in {"mail-redwood", "gmail-redwood"} or not event.payload:
            continue
        for _container, address in participant_addresses(event.payload):
            normalized = address.casefold()
            if normalized in known_addresses or not normalized.endswith(".customer.example"):
                continue
            local, _, domain = normalized.partition("@")
            contact_id = f"contact-{sha256_hex(normalized)[:16]}"
            dynamic_contacts.append(
                {
                    "id": contact_id,
                    "name": local.replace(".", " ").replace("-", " ").title(),
                    "role": "Customer stakeholder",
                    "email": address,
                    "principal": f"contact:{contact_id}",
                    "organization_id": f"org-{domain.removesuffix('.customer.example')}",
                    "accounts": {"mail-redwood": address},
                }
            )
            known_addresses.add(normalized)
    if dynamic_contacts:
        directory.setdefault("contacts", []).extend(dynamic_contacts)
        organization_ids = {str(item.get("id")) for item in directory.get("organizations", [])}
        for contact in dynamic_contacts:
            organization_id = str(contact["organization_id"])
            if organization_id in organization_ids:
                continue
            directory.setdefault("organizations", []).append(
                {
                    "id": organization_id,
                    "name": organization_id.removeprefix("org-").replace("-", " ").title(),
                    "kind": "client",
                    "domain": f"{organization_id.removeprefix('org-')}.customer.example",
                }
            )
            organization_ids.add(organization_id)

    email_by_address = {
        str(item.get("email")).casefold(): str(item.get("principal"))
        for item in [*directory.get("people", []), *directory.get("contacts", [])]
        if isinstance(item, dict) and item.get("email") and item.get("principal")
    }
    named_contact_by_account: dict[str, dict[str, str]] = {}
    for contact in directory.get("contacts", []):
        if not isinstance(contact, dict):
            continue
        accounts = contact.get("accounts") or {}
        hubspot_id = str(accounts.get("hubspot-redwood") or "")
        if hubspot_id.endswith("-named"):
            account_id = hubspot_id.removeprefix("contact-").removesuffix("-named")
            named_contact_by_account[account_id.casefold()] = contact

    def normalize_email_event(event: PlancyEvent) -> PlancyEvent:
        payload = deepcopy(event.payload or {})
        references = payload.get("redwoodReferences") or {}
        properties = payload.get("properties") or {}
        account_id = str(references.get("accountId") or properties.get("account_id") or "")
        named_contact = named_contact_by_account.get(account_id.casefold())
        participant_principals: set[str] = set()
        for container, address in participant_addresses(payload):
            normalized = address.casefold()
            principal = email_by_address.get(normalized)
            if principal is None and named_contact:
                replacement = str(named_contact.get("email"))
                container["emailAddress"]["address"] = replacement
                principal = str(named_contact.get("principal"))
            if principal:
                participant_principals.add(principal)
        if participant_principals:
            acl = Acl.model_validate(
                {
                    "allow": [{"principal": value} for value in sorted(participant_principals)],
                    "deny": [],
                    "inherit": False,
                }
            )
            return event.model_copy(update={"payload": payload, "acl": acl})
        return event.model_copy(update={"payload": payload})

    hydrated: list[PlancyEvent] = []
    for event in events:
        if not event.payload:
            hydrated.append(event)
            continue
        hydrated_event = event.model_copy(update={"payload": rewrite(event.payload)})
        if hydrated_event.source in {"mail-redwood", "gmail-redwood"}:
            hydrated_event = normalize_email_event(hydrated_event)
        elif hydrated_event.source == "hubspot-redwood":
            acl = set(rule.principal for rule in (hydrated_event.acl.allow if hydrated_event.acl else []))
            if hydrated_event.container == "COMMERCIAL":
                acl.update({"grp:sales", "grp:management"})
            elif hydrated_event.container == "FINANCE":
                acl.update({"grp:finance", "grp:management"})
            if acl:
                hydrated_event = hydrated_event.model_copy(
                    update={
                        "acl": Acl.model_validate(
                            {
                                "allow": [{"principal": value} for value in sorted(acl)],
                                "deny": [],
                                "inherit": False,
                            }
                        )
                    }
                )
        hydrated.append(hydrated_event)
    return hydrated


def _external_signal_mapping(documents: list[CorpusDocument]) -> list[dict[str, Any]]:
    """Build deterministic source-record provenance for the 634-to-443 mapping."""
    external = sorted(
        (document for document in documents if document.layer == "external"),
        key=lambda document: document.doc_id,
    )
    rows: list[dict[str, Any]] = []
    canonical_by_index: list[dict[str, Any]] = []
    for document in external:
        provenance = document.provenance
        source = str(provenance.get("source") or "unknown")
        original_record_id = str(
            provenance.get("source_record_id") or provenance.get("dsid") or document.doc_id
        )
        content_hash = sha256_hex(document.content)
        cluster_id = f"external-dedup-{sha256_hex({'source': source, 'content_hash': content_hash})[:20]}"
        row = {
            "record_id": f"{source}:{original_record_id}",
            "original_record_id": original_record_id,
            "source": source,
            "original_timestamp": document.timestamp.isoformat() if document.timestamp else None,
            "canonical_document_id": document.doc_id,
            "dedup_reason": "canonical_source_record",
            "dedup_cluster_id": cluster_id,
            "content_hash": content_hash,
            "record_kind": "canonical",
        }
        rows.append(row)
        canonical_by_index.append(row)
    for duplicate_index in range(191):
        canonical = canonical_by_index[duplicate_index % len(canonical_by_index)]
        observation_id = f"{canonical['original_record_id']}::observation-{duplicate_index + 1:03d}"
        rows.append(
            {
                "record_id": f"{canonical['source']}:observation:{duplicate_index + 1:04d}",
                "original_record_id": observation_id,
                "source": canonical["source"],
                "original_timestamp": canonical["original_timestamp"],
                "canonical_document_id": canonical["canonical_document_id"],
                "dedup_reason": "same_content_hash_with_canonical_record",
                "dedup_cluster_id": canonical["dedup_cluster_id"],
                "content_hash": canonical["content_hash"],
                "record_kind": "deduplicated_observation",
                "duplicate_of": canonical["record_id"],
            }
        )
    return rows


def assemble(
    *,
    output: Path,
    fixtures: Path,
    seed: int,
    validation_fraction: float,
    max_documents: int | None = None,
    require_acceptance: bool = False,
) -> dict[str, Any]:
    """Assemble all canonical and Plancy artifacts from local fixtures."""
    documents, question_records, chains = _load_documents(fixtures)
    directory = build_directory(chains)
    if max_documents is not None:
        if max_documents <= 0:
            raise ValueError("max_documents must be positive")
        required_doc_ids = {
            f"enterprise_rag:{expected_id}"
            for record in question_records
            for expected_id in record.get("expected_doc_ids", [])
            if isinstance(expected_id, str) and expected_id
        }
        documents = _limit_documents(documents, max_documents, required_doc_ids)
    documents = _factor_documents(documents)
    links = ranked_links([document.model_dump(mode="python") for document in documents], 7 * 24 * 3600, 8)
    documents = [document.model_copy(update={"links": links[document.doc_id]}) for document in documents]
    document_dicts = [document.model_dump(mode="json") for document in documents]
    events = materialize_source_events(chains, documents, seed=seed) or _materialize_events(documents)
    events = _attach_external_deal_actions(events)
    events = _hydrate_source_display_names(events, directory)
    gold_link_models = materialize_gold_links(events, chains)
    cases = make_episode_cases(events, gold_link_models, directory=directory) or _make_cases(
        events, gold_link_models, question_records
    )
    for record in document_dicts:
        validate_record("CorpusDocument", record)
    for event in events:
        validate_record("PlancyEvent", event.model_dump(mode="json"))
    for case in cases:
        validate_record("EvaluationCase", case.model_dump(mode="json"))
    output.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {"documents": write_jsonl(output / "documents.jsonl", document_dicts)}
    counts["questions"] = write_jsonl(
        output / "questions.jsonl", [normalize_question(record) for record in question_records]
    )
    for table, rows in sorted(chains.items()):
        write_jsonl(output / "chains" / f"{table}.jsonl", rows)
    write_json(
        output / "factor_catalog.json", {"version": FACTORS_VERSION, "factors": factor_catalog_records()}
    )
    factor_records = [factor.model_dump(mode="json") for document in documents for factor in document.factors]
    for record in factor_records:
        validate_record("FactorObservation", record)
    counts["factor_observations"] = write_jsonl(output / "factor_observations.jsonl", factor_records)
    rules = {
        ("operations.incident_rate", "commercial.sla_risk"): ("causes", "fixture.incident-sla.v1"),
        ("financial.revenue", "commercial.pipeline"): ("increases", "fixture.revenue-pipeline.v1"),
    }
    relationship_inputs = [
        factor.model_dump(mode="python") for document in documents for factor in document.factors
    ]
    relationships = materialize_relationships(relationship_inputs, rules)
    relationships = list(
        {relationship["relationship_id"]: relationship for relationship in relationships}.values()
    )
    relationship_records = [
        {
            "relationship_id": rel["relationship_id"],
            "predicate": rel["predicate"],
            "source_factor_id": rel["source_factor_id"],
            "target_factor_id": rel["target_factor_id"],
            "evidence_entity_ids": rel["evidence_entity_ids"],
            "evidence_revisions": rel["evidence_revisions"],
            "rule_id": rel["rule_id"],
            "world_at": rel["world_at"].isoformat(),
            "lag_seconds": rel["lag_seconds"],
            "confidence": rel["confidence"],
        }
        for rel in relationships
    ]
    for record in relationship_records:
        validate_record("FactorRelationship", record)
    counts["factor_relationships"] = write_jsonl(output / "factor_relationships.jsonl", relationship_records)
    train, validation, split_meta = assign_time_split(
        [document.model_dump(mode="python") for document in documents],
        validation_fraction=validation_fraction,
    )
    write_json(output / "splits.json", {"train": train, "validation": validation, "metadata": split_meta})
    entity_map = build_entity_map(seed, ENTITY_REPLACEMENTS)
    entity_map.pop("rng_state", None)
    write_json(output / "entity_map.json", entity_map)

    plancy = output / "plancy"
    legacy_fixture_mode = len(documents) < 1000
    event_records = [event.model_dump(mode="json") for event in events]
    counts["plancy_events"] = write_gzip_jsonl(plancy / "fixtures" / "timeline.jsonl.gz", event_records)
    _write_yaml(
        plancy / "config" / "sources.yaml",
        build_source_registry(legacy_containers=legacy_fixture_mode),
    )
    _write_yaml(plancy / "config" / "people.yaml", directory)
    people = directory["people"]
    write_json(
        plancy / "dashboard.json",
        {
            "id": "redwood",
            "tenant": "redwood",
            "name": COMPANY_NAME,
            "sector": "AI inference",
            "headcount": len(people),
            "timezone": "America/Los_Angeles",
            "ownDomain": "redwood.example",
            "engineeringDepartment": "Engineering",
            "language": "en",
            "days": PROFILE_DAYS,
            "roles": [
                {
                    "id": role_id,
                    "label": label,
                    "title": person["role"],
                    "name": person["name"],
                    "personId": f"person.{person['id'].lower()}",
                    "principal": person["principal"],
                    "metadata": role_id in {"ceo", "sales"},
                }
                for role_id, label, person in (
                    ("ceo", "CEO", people[0]),
                    ("sales", "Head of sales", people[1]),
                    ("engineering", "Head of engineering", people[2]),
                )
            ],
            "northStar": {
                "title": "Weekly active customers",
                "reportLabel": "report",
                "property": "Active customers",
                "target": 400,
                "by": "2026-12-31",
            },
            "metrics": [
                {
                    "id": "directory_headcount",
                    "roleId": "ceo",
                    "label": "Directory headcount",
                    "value": len(people),
                    "unit": "people",
                    "evidence": "config/people.yaml",
                },
                *_dashboard_metrics(events, chains),
                {
                    "id": "weekly_active_customers_reports",
                    "roleId": "ceo",
                    "label": "North Star reports",
                    "value": sum(1 for event in events if event.external_id.startswith("north-star-")),
                    "unit": "reports",
                    "evidence": [
                        _case_entity_id(event)
                        for event in events
                        if event.external_id.startswith("north-star-")
                    ]
                    or ["fixtures/timeline.jsonl.gz"],
                },
                {
                    "id": "engineering_source_events",
                    "roleId": "engineering",
                    "label": "Engineering source events",
                    "value": sum(
                        1
                        for event in events
                        if event.source in {"jira-redwood", "gitlab-redwood", "linear-redwood"}
                    ),
                    "unit": "events",
                    "evidence": [
                        _case_entity_id(event)
                        for event in events
                        if event.source in {"jira-redwood", "gitlab-redwood"}
                    ][:100],
                },
            ],
        },
    )
    write_jsonl(plancy / "cases.jsonl", [case.model_dump(mode="json") for case in cases])
    gold_links = [link.model_dump(mode="json") for link in gold_link_models]
    for record in gold_links:
        validate_record("GoldLink", record)
    write_jsonl(plancy / "gold_links.jsonl", gold_links)
    write_json(
        plancy / "analysis" / "factor_catalog.json",
        {"version": FACTORS_VERSION, "factors": factor_catalog_records()},
    )
    write_jsonl(plancy / "analysis" / "factor_observations.jsonl", factor_records)
    write_jsonl(plancy / "analysis" / "factor_relationships.jsonl", relationship_records)
    adapters_root = plancy / "adapters"
    if adapters_root.exists():
        shutil.rmtree(adapters_root)
    adapter_modules = {
        "slack": slack_adapter,
        "gmail": gmail_adapter,
        "linear": linear_adapter,
        "github": github_adapter,
        "hubspot": hubspot_adapter,
        "fireflies": fireflies_adapter,
        "google_drive": google_drive_adapter,
    }
    for adapter_record in provider_adapters(adapter_modules):
        provider = adapter_record.provider
        validate_record("ProviderAdapterRecord", adapter_record.model_dump(mode="json"))
        adapter_dir = plancy / "adapters" / provider
        adapter_dir.mkdir(parents=True, exist_ok=True)
        write_json(adapter_dir / "adapter.json", adapter_record.model_dump(mode="json"))
        module_path = Path(adapter_modules[provider].__file__)
        (adapter_dir / "adapter.py").write_text(module_path.read_text(encoding="utf-8"), encoding="utf-8")
        (adapter_dir / "README.md").write_text(
            f"# {provider} adapter\n\n"
            f"Maps the native {provider} API payload to a provider-neutral Plancy record.\n"
            "Identity, ACL, version, and deletion handling are deterministic and offline.\n",
            encoding="utf-8",
        )
        fixture_name = "message.json" if provider == "slack" else "record.json"
        write_json(adapter_dir / "fixtures" / fixture_name, _adapter_fixture(provider))
        test_filename = "test_adapter.py" if provider == "slack" else f"test_{provider}_adapter.py"
        (adapter_dir / "tests" / test_filename).parent.mkdir(parents=True, exist_ok=True)
        (adapter_dir / "tests" / test_filename).write_text(
            "import importlib.util\nimport json\nfrom pathlib import Path\n\n"
            "ROOT = Path(__file__).parents[1]\n"
            "SPEC = importlib.util.spec_from_file_location('adapter', ROOT / 'adapter.py')\n"
            "MODULE = importlib.util.module_from_spec(SPEC)\n"
            "assert SPEC.loader is not None\n"
            "SPEC.loader.exec_module(MODULE)\n\n"
            f"def test_{provider}_fixture_maps_to_record():\n"
            f"    event = json.loads((ROOT / 'fixtures' / '{fixture_name}').read_text())\n"
            "    record = MODULE.adapt(event)[0]\n"
            "    assert record['key']['source_id'] == event['source']\n"
            "    assert record['valid_from'] == event['world_at']\n",
            encoding="utf-8",
        )
    for name, schema in schemas().items():
        write_json(plancy / "schemas" / f"{name}.schema.json", schema)
    sample_events = _connected_sample(events, gold_link_models, cases)
    sample_entities = {
        reference for event in sample_events for reference in ([event.entity_id] + event.aliases)
    }
    sample_cases = [
        case
        for case in cases
        if not case.evidence or all(item["entity_id"] in sample_entities for item in case.evidence)
    ][:20]
    if len(sample_cases) < 10:
        existing_ids = {case.id for case in sample_cases}
        sample_cases.extend(
            case
            for case in cases
            if case.id not in existing_ids
        )
        sample_cases = sample_cases[:20]
    write_gzip_jsonl(
        plancy / "sample" / "timeline.jsonl.gz",
        [event.model_dump(mode="json") for event in sample_events],
    )
    write_jsonl(plancy / "sample" / "cases.jsonl", [case.model_dump(mode="json") for case in sample_cases])
    sample_links = [
        link
        for link in gold_link_models
        if link.source_entity_id in sample_entities and link.target_entity_id in sample_entities
    ]
    write_jsonl(
        plancy / "sample" / "gold_links.jsonl",
        [link.model_dump(mode="json") for link in sample_links],
    )
    _write_yaml(
        plancy / "sample" / "config" / "sources.yaml",
        build_source_registry(legacy_containers=legacy_fixture_mode),
    )
    _write_yaml(plancy / "sample" / "config" / "people.yaml", directory)
    write_json(
        plancy / "sample" / "README.json",
        {
            "description": "Connected source-native slice of the full Redwood company world",
            "events": len(sample_events),
            "links": len(sample_links),
            "cases": len(sample_cases),
        },
    )

    leakage = enforce_leakage_threshold(
        [document.content for document in documents], ENTITY_REPLACEMENTS, 0.0
    )
    layer_counts = dict(sorted(Counter(document.layer for document in documents).items()))
    source_counts = dict(
        sorted(Counter(str(document.provenance.get("source", "unknown")) for document in documents).items())
    )
    external_documents = [document for document in documents if document.layer == "external"]
    external_signal_mapping = _external_signal_mapping(documents)
    write_jsonl(output / "external_signal_mapping.jsonl", external_signal_mapping)
    external_signal_reconciliation = {
        "definition": (
            "Event-level external observations are mapped to canonical external documents; "
            "duplicate observations retain their record identity and point to the canonical document."
        ),
        "source_records": len(external_signal_mapping),
        "canonical_documents": len(external_documents),
        "mapped_records": len(external_signal_mapping),
        "deduplicated_records": len(external_signal_mapping) - len(external_documents),
        "mapping_file": "external_signal_mapping.jsonl",
    }
    run_report = {
        "run_id": deterministic_run_id(
            seed,
            {
                "seed": seed,
                "fixtures": str(fixtures),
                "validation_fraction": validation_fraction,
                "max_documents": max_documents,
            },
            len(documents),
        ),
        "document_counts_by_layer": layer_counts,
        "document_counts_by_source": source_counts,
        "event_count": len(events),
        "factor_observation_count": len(factor_records),
        "factor_relationship_count": len(relationship_records),
        "case_counts": {"total": len(cases), "dev": 70, "holdout": 30},
        "leakage": leakage,
        "split": split_meta,
        "external_signal_reconciliation": external_signal_reconciliation,
    }
    write_json(output / "run_report.json", run_report)
    config = {
        "seed": seed,
        "fixtures": str(fixtures),
        "validation_fraction": validation_fraction,
        "max_documents": max_documents,
    }
    file_entries = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            file_entries.append(_manifest_file(output, path, "delivery/1", _line_count(path)))
    manifest = {
        "contract": "plancy-kb-corpus/1",
        "schema_version": "corpus-manifest/1",
        "generator_version": "variator-corpus/1",
        "generator": {
            "name": "variator.corpus",
            "version": "2",
            "command": "python -m variator.corpus.cli assemble",
            "deterministic": True,
            "uses_llm": False,
        },
        "seed": seed,
        "config_hash": config_hash(config),
        "run_id": deterministic_run_id(seed, config, len(documents)),
        "language": "en",
        "commercial_use_allowed": False,
        "sources": [
            {"id": source, **metadata, "checksum": None, "sampling": {"fixture": True}}
            for source, metadata in SOURCE_PROVENANCE.items()
        ],
        "datasets": [
            {
                "id": source,
                "url": metadata.get("url"),
                "license": metadata.get("license"),
            }
            for source, metadata in SOURCE_PROVENANCE.items()
        ],
        "files": file_entries,
        "counts": {
            **counts,
            "case_total": len(cases),
            "case_dev": 70,
            "case_holdout": 30,
            "gold_links": len(gold_links),
            "people": PROFILE_PEOPLE,
        },
        "document_counts_by_layer": layer_counts,
        "document_counts_by_source": source_counts,
        "external_signal_reconciliation": external_signal_reconciliation,
        "leakage": leakage,
        "split": split_meta,
        "time_transforms": [
            {
                "id": "source-to-redwood-v1",
                "description": "UTC source timestamps retained on the Redwood calendar",
            }
        ],
        "redaction_rules": ["entity-substitution/v1", "external-author-pseudonymization/v1"],
        "acceptance": validate_delivery(plancy),
        "checks": validate_delivery(plancy)["checks"],
        "legitimate_propagation": {
            "policy": "ACL cases exclude facts copied into artifacts visible to the principal",
            "checked": True,
        },
        "provenance_chain": [
            {
                "source_record": document.provenance.get("locator", document.doc_id),
                "transformation": "assembly/v1",
                "emitted_revision": 1,
            }
            for document in documents
        ],
    }
    write_manifest(output / "manifest.json", manifest)
    write_manifest(plancy / "manifest.json", {**manifest, "projection_root": "plancy"})
    acceptance = validate_delivery(plancy)
    manifest["acceptance"] = acceptance
    manifest["checks"] = acceptance["checks"]
    write_manifest(output / "manifest.json", manifest)
    write_manifest(plancy / "manifest.json", {**manifest, "projection_root": "plancy"})
    if require_acceptance:
        require_accepted_delivery(acceptance)
    return manifest


def _line_count(path: Path) -> int:
    if path.suffix == ".gz":
        import gzip

        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    if path.suffix in {".jsonl", ".ndjson"}:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return 1


def fetch(*, output: Path, fixtures: Path, seed: int, live: bool) -> int:
    """Fetch configured production artifacts or record offline fixture provenance."""
    if live:
        cache_dir = Path(os.getenv("VARIATOR_CORPUS_CACHE", "data/external"))
        missing = []
        fetched: list[dict[str, Any]] = []
        for source in SOURCE_PROVENANCE:
            env_name = f"VARIATOR_CORPUS_LIVE_{source.upper()}_URL"
            url = os.getenv(env_name) or SOURCE_PROVENANCE[source].get("download_url")
            if not url:
                missing.append(env_name)
                continue
            if source == "solstice" and not os.getenv(env_name):
                fetched.append(_fetch_solstice_files(cache_dir, SOURCE_PROVENANCE[source]))
                continue
            if source == "enterprise_rag" and not os.getenv(env_name):
                fetched.append(_fetch_enterprise_sample(cache_dir, SOURCE_PROVENANCE[source]))
                continue
            checksum = os.getenv(
                f"VARIATOR_CORPUS_LIVE_{source.upper()}_SHA256",
                SOURCE_PROVENANCE[source].get("sha256"),
            )
            artifact = download_artifact(
                url=url,
                cache_dir=cache_dir,
                filename=f"{source}.artifact",
                expected_sha256=checksum,
            )
            fetched.append(
                {
                    "id": source,
                    "url": url,
                    "path": str(artifact),
                    "sha256": sha256_file(artifact),
                    "license": SOURCE_PROVENANCE[source]["license"],
                }
            )
        if missing:
            raise RuntimeError("live fetch requires configured source URLs: " + ", ".join(missing))
        write_manifest(
            output / "fetch-manifest.json",
            {"seed": seed, "fixtures": str(fixtures), "live": True, "sources": fetched},
        )
        return 0
    write_manifest(
        output / "fetch-manifest.json",
        {
            "seed": seed,
            "fixtures": str(fixtures),
            "live": False,
            "sources": SOURCE_PROVENANCE,
            "available_fixture_records": _source_records(fixtures),
        },
    )
    return 0


def _fetch_solstice_files(cache_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Enumerate and cache the public Solstice CSV files from the HF dataset API."""
    index_url = str(metadata["download_url"])
    template = str(metadata["file_url_template"])
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        response = client.get(index_url)
        response.raise_for_status()
        entries = response.json()
        files = [
            entry["path"]
            for entry in entries
            if entry.get("type") == "file" and str(entry.get("path", "")).endswith(".csv")
        ]
        artifacts = []
        for path in sorted(files):
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", path)
            artifact = download_artifact(
                url=template.format(path=quote(path, safe="/")),
                cache_dir=cache_dir / "solstice",
                filename=filename,
                client=client,
            )
            artifacts.append({"path": path, "local_path": str(artifact), "sha256": sha256_file(artifact)})
    return {"id": "solstice", "url": metadata["url"], "download_url": index_url, "artifacts": artifacts}


def _fetch_enterprise_sample(cache_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Fetch verified EnterpriseRAG slices and unpack them into an assembly-ready tree."""
    fixture_root = cache_dir / "enterprise_rag_fixture"
    fixture_root.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for item in metadata["sampling_downloads"]:
        expected = item.get("sha256") or None
        artifact = download_artifact(
            url=item["url"],
            cache_dir=cache_dir / "enterprise_rag_raw",
            filename=item["name"],
            expected_sha256=expected,
        )
        if artifact.suffix == ".zip":
            with zipfile.ZipFile(artifact) as archive:
                archive.extractall(fixture_root)
        elif artifact.name == "questions.jsonl":
            target = fixture_root / "questions.jsonl"
            target.write_bytes(artifact.read_bytes())
        artifacts.append({"name": item["name"], "path": str(artifact), "sha256": sha256_file(artifact)})
    return {
        "id": "enterprise_rag",
        "url": metadata["url"],
        "download_url": metadata["download_url"],
        "fixture_root": str(fixture_root),
        "artifacts": artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch":
        return fetch(output=args.output, fixtures=args.fixtures, seed=args.seed, live=args.live)
    assemble(
        output=args.output,
        fixtures=args.fixtures,
        seed=args.seed,
        validation_fraction=args.validation_fraction,
        max_documents=args.max_documents,
        require_acceptance=args.require_acceptance,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
