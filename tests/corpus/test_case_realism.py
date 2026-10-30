import re

from test_episode_cases import _case_data


def test_cases_use_role_aware_principals_and_multi_record_facts() -> None:
    _, cases, _, _ = _case_data()
    assert all(case.principal != "user:admin" for case in cases)
    numeric = [case for case in cases if case.category == "numeric"]
    assert all(len(case.evidence) >= 6 for case in numeric)
    assert all(
        {"invoice_total", "paid_total", "outstanding_total"}.issubset(
            {fact["key"] for fact in case.answer_facts}
        )
        for case in numeric
    )
    why = [case for case in cases if case.category == "why"]
    assert all(len(case.evidence) == 9 and len(case.roots) >= 3 for case in why)

    identifier = re.compile(
        r"\b(?=[A-Z0-9-]*\d)[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b",
        re.IGNORECASE,
    )
    normalized = [identifier.sub("ID", case.question) for case in cases]
    assert len(set(normalized)) / len(normalized) > 0.8
    assert all(case.question[0].isupper() and case.question.endswith("?") for case in cases)
    malformed = ("report is the", "trace why was", "reconcile does", "state is the")
    assert all(not any(fragment in case.question.casefold() for fragment in malformed) for case in cases)


def test_non_negative_cases_use_concrete_business_facts() -> None:
    _, cases, _, _ = _case_data()
    concrete_fragments = {
        "amount",
        "date",
        "reason",
        "owner",
        "status",
        "invoice_total",
        "paid_total",
        "outstanding_total",
        "decision",
        "deployment",
    }
    for case in cases:
        if case.category == "negative":
            continue
        keys = {fact["key"] for fact in case.answer_facts}
        assert any(fragment in key for key in keys for fragment in concrete_fragments)
        assert keys != {"episode_id"}
