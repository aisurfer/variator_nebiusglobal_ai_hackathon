"""EnterpriseRAG question metadata preserved beside canonical documents."""

from __future__ import annotations

from typing import Any


def normalize_question(record: dict[str, Any]) -> dict[str, Any]:
    """Return the joinable question and its original EnterpriseRAG ``dsid``."""
    dsid = record.get("dsid")
    if dsid is None:
        expected_doc_ids = record.get("expected_doc_ids")
        if isinstance(expected_doc_ids, list) and expected_doc_ids:
            dsid = expected_doc_ids[0]
        else:
            dsid = record.get("question_id")
    question = record.get("question")
    if not isinstance(dsid, str) or not dsid.strip():
        raise ValueError("EnterpriseRAG question requires dsid")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("EnterpriseRAG question requires question text")
    normalized = dict(record)
    normalized["dsid"] = dsid
    normalized["question"] = question
    return normalized
