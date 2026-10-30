"""What changes an actor: the case items (decisions, hypotheses, shocks) that touch it.

The labels and texts come from the case configs (``workspace/config/cases/<case>.json``);
this module only says which actors each item touches. A selector is a card id
(``VN-03``, ``LW-E04``, ``clients:standard``) or a group: ``@engineers`` (current staff in
engineer roles), ``@staff`` (all current staff), ``@key_clients``, ``@future_engineers``,
``@future`` (every future-hire cohort), ``@company``.

``RISK_SHOCKS`` maps a risk of the register (by its stable ``code``) to the case items that
model it as a shock or a hypothesis.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# case id -> item id -> selectors
TOUCHES: dict[str, dict[str, list[str]]] = {
    "company_best_year": {
        # decisions (forks)
        "pay_tech_debt": ["@engineers", "@company"],
        "hire_5": ["@future_engineers", "@engineers", "@company"],
        "reserve_gpu": ["VN-03", "@company"],
        "raise_prices_8": ["@key_clients", "clients:standard", "prospects", "@company"],
        # hypotheses about the world
        "price_war": ["prospects", "clients:standard", "@key_clients"],
        "gpu_crunch": ["VN-03"],
        "big_client_leaves": ["@key_clients"],
        "client_requirements_churn": ["@key_clients", "clients:standard", "@engineers"],
        "hiring_freeze": ["@future", "@staff"],
        "cash_squeeze": ["@key_clients", "clients:standard", "@company"],
        "downturn": ["prospects"],
        # shocks
        "gpu_shortage_event": ["VN-03"],
        "vendor_price_event": ["VN-03"],
        "fx_event": ["VN-02"],
        "regulation_event": ["TA-UK"],
        "competitor_event": ["prospects", "clients:standard", "@key_clients"],
        "key_person_event": ["@engineers"],
        "key_account_event": ["@key_clients"],
        "reputation_event": ["@key_clients", "clients:standard"],
        "macro_event": ["prospects"],
    },
    "delivery_best_way": {
        "in_house": ["@engineers", "@future_engineers"],
        "nearshore_tm": ["VN-01"],
        "offshore_fixed": ["VN-02"],
        "requirements_change": ["LW-E04", "LW-E05"],
        "priorities_shift": ["@engineers"],
        "key_people_leave": ["@engineers", "VN-01", "VN-02"],
        "vendor_rates_rise": ["VN-01", "VN-02"],
        "tight_budget": ["LW-E02"],
        "key_person_loss": ["@engineers"],
        "vendor_turnover": ["VN-01", "VN-02"],
        "scope_creep": ["LW-E04"],
    },
}

# risk code -> [(case id, item id)]
RISK_SHOCKS: dict[str, list[tuple[str, str]]] = {
    "incident_sev1": [("company_best_year", "reputation_event")],
    "vendor_capacity": [("company_best_year", "gpu_shortage_event"), ("company_best_year", "gpu_crunch")],
    "client_churn": [
        ("company_best_year", "key_account_event"),
        ("company_best_year", "big_client_leaves"),
        ("company_best_year", "competitor_event"),
    ],
    "late_payment_spell": [("company_best_year", "cash_squeeze")],
    "deal_lost": [
        ("company_best_year", "competitor_event"),
        ("company_best_year", "macro_event"),
        ("company_best_year", "price_war"),
        ("company_best_year", "downturn"),
    ],
    "resignation": [
        ("company_best_year", "key_person_event"),
        ("company_best_year", "hiring_freeze"),
        ("delivery_best_way", "key_person_loss"),
        ("delivery_best_way", "key_people_leave"),
    ],
    "key_person_absence": [("company_best_year", "key_person_event")],
    "team_overload": [("company_best_year", "client_requirements_churn")],
    "vendor_cost_change": [
        ("company_best_year", "vendor_price_event"),
        ("company_best_year", "fx_event"),
        ("delivery_best_way", "vendor_rates_rise"),
    ],
}


def load_cases(cases_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Case configs by id (the files listed in ``index.json``; previews and origins are left out)."""
    root = Path(cases_dir)
    out: dict[str, dict[str, Any]] = {}
    for cid in TOUCHES:
        path = root / f"{cid}.json"
        if path.is_file():
            out[cid] = json.loads(path.read_text(encoding="utf-8"))
    return out


def case_items(case: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Every decision, hypothesis and shock of a case config: id -> {kind, label, text}."""
    items: dict[str, dict[str, str]] = {}
    for f in case.get("forks") or []:
        items[f["id"]] = {
            "kind": "decision",
            "label": f.get("label") or f["id"],
            "text": f.get("description") or "",
        }
    for h in case.get("hypotheses") or []:
        effects = "; ".join(h.get("effects") or [])
        items[h["id"]] = {
            "kind": "hypothesis",
            "label": h.get("label") or h["id"],
            "text": h.get("description") or effects,
        }
    for sid, s in (case.get("shocks") or {}).items():
        affects = ", ".join(a.get("label", "") for a in s.get("affects") or [])
        items[sid] = {
            "kind": "shock",
            "label": s.get("label") or sid,
            "text": f"Affects: {affects}" if affects else "",
        }
    return items


def missing_items(cases: dict[str, dict[str, Any]]) -> list[str]:
    """Mapped items that the case configs do not have (a stale map): ``case/item``."""
    out = []
    for cid, touches in TOUCHES.items():
        if cid not in cases:
            continue
        have = case_items(cases[cid])
        out += [f"{cid}/{i}" for i in touches if i not in have]
    for code, refs in RISK_SHOCKS.items():
        for cid, i in refs:
            if cid in cases and i not in case_items(cases[cid]):
                out.append(f"{code}: {cid}/{i}")
    return out


def _entry(cid: str, case: dict[str, Any], iid: str, item: dict[str, str]) -> dict[str, str]:
    return {
        "case": str(case.get("letter") or cid),
        "caseId": cid,
        "caseTitle": str(case.get("title") or cid),
        "kind": item["kind"],
        "id": iid,
        "label": item["label"],
        "text": item["text"],
    }


def changes_by_card(
    cases: dict[str, dict[str, Any]], groups: dict[str, list[str]]
) -> dict[str, list[dict[str, str]]]:
    """Card id -> the case items that touch it, in case and item order."""
    out: dict[str, list[dict[str, str]]] = {}
    for cid, touches in TOUCHES.items():
        case = cases.get(cid)
        if case is None:
            continue
        items = case_items(case)
        for iid, selectors in touches.items():
            if iid not in items:
                continue
            targets: list[str] = []
            for s in selectors:
                targets += groups.get(s, []) if s.startswith("@") else [s]
            for t in dict.fromkeys(targets):
                out.setdefault(t, []).append(_entry(cid, case, iid, items[iid]))
    return out


def shocks_by_risk(cases: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Risk code -> the case items that model it."""
    out: dict[str, list[dict[str, str]]] = {}
    for code, refs in RISK_SHOCKS.items():
        for cid, iid in refs:
            case = cases.get(cid)
            item = case_items(case).get(iid) if case else None
            if item:
                out.setdefault(code, []).append(_entry(cid, case, iid, item))
    return out
