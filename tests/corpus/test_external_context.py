from datetime import UTC, datetime

from variator.corpus.cli import _external_signal_mapping
from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.models import CorpusDocument
from variator.corpus.source_events import materialize_source_events


def test_external_records_are_used_by_operational_actions() -> None:
    documents = [
        CorpusDocument(
            doc_id="external:news:001",
            layer="external",
            source_type="news",
            timestamp=datetime(2026, 2, 1, tzinfo=UTC),
            author="E001",
            content="Competitor launched a lower-cost product.",
            provenance={"source": "cc_news", "dsid": "news-001", "title": "Competitor launch"},
        ),
        CorpusDocument(
            doc_id="external:reddit:001",
            layer="external",
            source_type="reddit",
            timestamp=datetime(2026, 2, 2, tzinfo=UTC),
            author="E002",
            content="Customer reports a painful onboarding issue.",
            provenance={"source": "reddit", "dsid": "reddit-001", "title": "Onboarding complaint"},
        ),
        CorpusDocument(
            doc_id="external:review:001",
            layer="external",
            source_type="review",
            timestamp=datetime(2026, 2, 3, tzinfo=UTC),
            author="E003",
            content="The integration failed during setup.",
            provenance={
                "source": "amazon_reviews_2023",
                "dsid": "review-001",
                "title": "Integration review",
            },
        ),
    ]
    events = materialize_source_events({"employees": [{"employee_id": "E001"}]}, documents, seed=42)
    actions = [event for event in events if event.payload and event.payload.get("redwoodExternalAction")]
    assert {event.payload["externalSource"] for event in actions} == {
        "cc_news",
        "reddit",
        "amazon_reviews_2023",
    }
    assert any(event.source == "fireflies-redwood" for event in actions)
    assert any(event.source == "confluence-redwood" for event in actions)
    assert all(event.payload.get("canonicalDocumentId") for event in actions)

    links = materialize_gold_links(events, {})
    action_aliases = {event.aliases[0] for event in actions}
    assert action_aliases.issubset({link.source_entity_id for link in links})


def test_external_mapping_keeps_source_record_provenance() -> None:
    documents = [
        CorpusDocument(
            doc_id="external:review:001",
            layer="external",
            source_type="review",
            timestamp=datetime(2026, 2, 3, tzinfo=UTC),
            author=None,
            content="A review with a stable source record.",
            provenance={
                "source": "amazon_reviews_2023",
                "source_record_id": "review-001",
            },
        )
    ]
    rows = _external_signal_mapping(documents)
    assert len(rows) == 192
    required = {
        "original_record_id",
        "source",
        "original_timestamp",
        "canonical_document_id",
        "dedup_reason",
        "dedup_cluster_id",
        "content_hash",
    }
    assert required.issubset(rows[0])
    assert rows[0]["original_record_id"] == "review-001"
    assert rows[0]["dedup_reason"] == "canonical_source_record"
    assert all(row["canonical_document_id"] == "external:review:001" for row in rows)
    assert all(row["content_hash"] == rows[0]["content_hash"] for row in rows)
    assert all(row["dedup_reason"] == "same_content_hash_with_canonical_record" for row in rows[1:])


def test_external_implicit_quotes_are_natural_and_target_free() -> None:
    documents = [
        CorpusDocument(
            doc_id="external:review:002",
            layer="external",
            source_type="review",
            timestamp=datetime(2026, 2, 4, tzinfo=UTC),
            author="E003",
            content="The customer review describes a setup failure.",
            provenance={
                "source": "amazon_reviews_2023",
                "source_record_id": "review-002",
                "title": "Customer review",
            },
        )
    ]
    events = materialize_source_events({"employees": [{"employee_id": "E003"}]}, documents, seed=43)
    links = materialize_gold_links(events, {})
    implicit = [link for link in links if not link.explicit]
    assert len(implicit) >= 1
    assert all(link.evidence_quote for link in implicit)
    assert all("the the" not in link.evidence_quote.casefold() for link in implicit)
    assert all("handoff handoff" not in link.evidence_quote.casefold() for link in implicit)
    assert all("amazon reviews 2023" not in link.evidence_quote.casefold() for link in implicit)
    assert all(
        link.target_entity_id.split("-")[-1].casefold() not in link.evidence_quote.casefold()
        for link in implicit
    )
