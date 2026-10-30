"""EnterpriseRAG thematic graph distribution contract."""

from datetime import UTC, datetime, timedelta

from variator.corpus.gold_links import materialize_gold_links
from variator.corpus.models import CorpusDocument
from variator.corpus.source_events import materialize_source_events


def _documents() -> list[CorpusDocument]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        CorpusDocument(
            doc_id=f"enterprise_rag:github:{index:04d}",
            layer="internal",
            source_type="github",
            timestamp=start + timedelta(days=index % 180),
            author="engineer",
            content=f"Implement connector behavior for work item {index}.",
            provenance={
                "source": "enterprise_rag",
                "dsid": f"github:{index:04d}",
                "title": f"Work item {index}",
            },
        )
        for index in range(120)
    ]


def test_enterprise_changes_are_distributed_across_thematic_roots() -> None:
    events = materialize_source_events({}, _documents(), seed=42)
    pages = {
        event.external_id
        for event in events
        if event.source == "confluence-redwood" and event.external_id.startswith("enterprise-")
    }
    tasks = [
        event for event in events if event.source == "jira-redwood" and event.external_id.startswith("ER-")
    ]

    assert len({item for item in pages if item.startswith("enterprise-project-")}) >= 50
    assert len({item for item in pages if item.startswith("enterprise-epic-")}) >= 50
    assert len({item for item in pages if item.startswith("enterprise-decision-")}) >= 50
    assert tasks
    assert all("enterprise-engineering-rollout" not in str(event.payload) for event in tasks)
    assert (
        len(
            {
                event.payload["redwoodTheme"]["decisionId"]
                for event in events
                if event.source == "gitlab-redwood"
                and event.external_id.startswith("github-")
                and event.revision == 1
            }
        )
        >= 50
    )


def test_enterprise_gold_links_use_thematic_sources() -> None:
    events = materialize_source_events({}, _documents(), seed=42)
    links = materialize_gold_links(events, {})
    thematic = [
        link
        for link in links
        if link.rule_id
        in {
            "enterprise_task.decision.v3",
            "enterprise_task.epic.v3",
            "enterprise_task.project.v3",
        }
    ]
    assert len(thematic) >= 120 * 3
    assert len({link.target_entity_id for link in thematic}) >= 50
    assert all(link.evidence_quote.startswith("enterprise-") for link in thematic)
