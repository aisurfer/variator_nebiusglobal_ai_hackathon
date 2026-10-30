from collections import Counter
from datetime import UTC, datetime

from variator.corpus.models import CorpusDocument
from variator.corpus.source_events import materialize_source_events


def test_operational_events_follow_business_rhythm() -> None:
    document = CorpusDocument(
        doc_id="enterprise_rag:confluence:rhythm",
        layer="internal",
        source_type="confluence",
        timestamp=datetime(2026, 1, 3, tzinfo=UTC),
        author="E001",
        content="A normal operational record.",
        provenance={"source": "enterprise_rag", "dsid": "confluence:rhythm", "title": "Rhythm"},
    )
    events = materialize_source_events({"employees": [{"employee_id": "E001"}]}, [document], seed=42)
    times = [event.world_at for event in events]
    assert times
    assert sum(value.weekday() >= 5 for value in times) / len(times) < 0.05
    assert sum(not 8 <= value.hour < 18 for value in times) / len(times) < 0.05
    assert len(Counter(value.hour for value in times)) >= 3
