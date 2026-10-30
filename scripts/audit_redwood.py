#!/usr/bin/env python3
"""Canonical, manifest-independent audit for a Redwood Plancy delivery."""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import re
import sys
import tarfile
from datetime import datetime
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit raw Redwood Plancy delivery evidence")
    parser.add_argument("root", type=Path, help="path to the delivery/plancy directory")
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        help="also write the machine-readable report to this path",
    )
    parser.add_argument(
        "--format",
        choices=("human", "json", "both"),
        default="both",
        help="output format; default is human output followed by JSON",
    )
    parser.add_argument(
        "--archive", type=Path, help="optional tar.gz archive to audit before delivery acceptance"
    )
    return parser


def _archive_audit(path: Path | None) -> tuple[bool, dict[str, object]]:
    if path is None:
        return True, {"archive_checked": False, "archive_metadata_violations": 0}
    violations = 0
    details: dict[str, object] = {"archive_checked": True, "archive_path": str(path)}
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
            metadata_members = [name for name in names if name.startswith("._") or "/._" in name]
            macos_members = [name for name in names if name == "__MACOSX" or name.startswith("__MACOSX/")]
            manifest_names = [
                name for name in names if name.endswith("/manifest.json") or name == "manifest.json"
            ]
            manifest_accepted = False
            if manifest_names:
                manifest = json.load(archive.extractfile(manifest_names[0]))
                manifest_accepted = manifest.get("acceptance", {}).get("accepted") is True
            violations = len(metadata_members) + len(macos_members) + int(not manifest_accepted)
            details.update(
                {
                    "archive_member_count": len(names),
                    "macos_metadata_members": metadata_members,
                    "__MACOSX_members": macos_members,
                    "manifest_accepted": manifest_accepted,
                }
            )
    except (OSError, tarfile.TarError, json.JSONDecodeError, AttributeError) as exc:
        violations = 1
        details["archive_error"] = f"{type(exc).__name__}: {exc}"
    details["archive_metadata_violations"] = violations
    return violations == 0, details


def _human(report: dict[str, object], root: Path) -> str:
    checks = report.get("checks", {})
    failed = report.get("failed", [])
    lines = [
        f"Redwood canonical audit: {'PASS' if report.get('accepted') is True else 'FAIL'}",
        f"Root: {root}",
        "Checks:",
    ]
    for name in sorted(checks):
        lines.append(f"  {'PASS' if checks[name] else 'FAIL'} {name}")
    lines.append(f"Failed thresholds: {len(failed)}")
    metrics = report.get("metrics")
    if isinstance(metrics, dict):
        lines.append(
            "Observed: "
            f"events={metrics.get('event_count', 0)}, "
            f"latest_entities={metrics.get('latest_entity_count', 0)}, "
            f"links={metrics.get('link_count', 0)}, "
            f"cases={metrics.get('case_count', 0)}, "
            f"external_signal_ids={metrics.get('external_signal_ids', 0)}"
        )
        lines.append(f"Manifest consulted: {metrics.get('manifest_consulted', True)}")
        for key in (
            "timeline_sort_violations",
            "payload_external_id_mismatches",
            "unresolved_gold_link_endpoints",
            "unresolved_case_references",
            "emails_without_body",
            "emails_without_acl",
            "broad_email_acl_count",
            "unknown_email_participants",
            "open_task_ratio",
            "reopened_task_count",
            "vacation_conflicts",
            "north_star_schema_violations",
            "meeting_protocol_schema_violations",
            "attachments_without_document_meta",
            "archive_metadata_violations",
        ):
            if key in metrics:
                lines.append(f"  {key}: {metrics[key]}")
    return "\n".join(lines)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read_gzip_jsonl(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for event in events:
        entity_id = str(event.get("entity_id", ""))
        if not entity_id:
            continue
        if event.get("op") == "delete":
            result.pop(entity_id, None)
        else:
            result[entity_id] = event
    return result


def _payload_text(event: dict[str, object]) -> str:
    return json.dumps(event.get("payload") or {}, ensure_ascii=False)


def _semantic_signature(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[a-z0-9_.+-]+@[a-z0-9.-]+", "<email>", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:t[^\s]+)?\b", "<date>", text)
    text = re.sub(r"\b[a-f0-9]{7,64}\b", "<id>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", text)
    text = re.sub(r"\b(?:e\d{3}|rw-e\d{3}|acct:e\d{3})\b", "<person>", text)
    text = re.sub(r"\b(?:p|ep|pr|task|mr|inv|quote|contract|deal|a|o)\d+[a-z0-9-]*\b", "<id>", text)
    return re.sub(r"\s+", " ", text).strip()


def _distribution(values: list[int]) -> dict[str, object]:
    if not values:
        return {"count": 0, "min": 0, "max": 0, "mean": 0.0, "histogram": {}}
    counts = collections.Counter(values)
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 3),
        "histogram": {str(key): counts[key] for key in sorted(counts)},
    }


def _raw_metrics(
    root: Path,
    events: list[dict[str, object]],
    links: list[dict[str, object]],
    cases: list[dict[str, object]],
) -> dict[str, object]:
    latest = _latest(events)
    latest_events = list(latest.values())
    payload_text_by_event = {
        id(event): json.dumps(
            event.get("payload") or {}, ensure_ascii=False, separators=(",", ":")
        ).casefold()
        for event in latest_events
    }
    by_reference = {
        reference: event
        for event in latest_events
        for reference in [
            str(event.get("entity_id", "")),
            str(event.get("external_id", "")),
            *(str(value) for value in event.get("aliases", [])),
        ]
        if reference
    }
    stage_times: dict[str, dict[str, list[datetime]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for event in latest_events:
        payload = event.get("payload") or {}
        chain = payload.get("redwoodCausalChainId") or payload.get("redwoodExternalChainId")
        stage = payload.get("redwoodStage") or payload.get("redwoodExternalStage")
        when = _parse_time(event.get("world_at"))
        if chain and stage and when:
            stage_times[str(chain)][str(stage)].append(when)

    def chronology(prefix: str, stages: list[str]) -> dict[str, int]:
        selected = {key: value for key, value in stage_times.items() if key.startswith(prefix)}
        complete = 0
        violations = 0
        for stage_map in selected.values():
            present = [stage for stage in stages if stage in stage_map]
            if len(present) < 2:
                continue
            complete += 1
            points = [max(stage_map[stage]) for stage in present]
            violations += int(points != sorted(points) or len(set(points)) != len(points))
        return {"chains": len(selected), "complete": complete, "violations": violations}

    ordinary_stage: dict[str, set[str]] = collections.defaultdict(set)
    ordinary_times: dict[str, dict[str, list[datetime]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    ordinary_decisions: set[str] = set()
    ordinary_ordered = 0
    ordinary_complete = 0
    for event in latest_events:
        payload = event.get("payload") or {}
        work = str(payload.get("redwoodEnterpriseWorkId") or "")
        stage = str(payload.get("redwoodEnterpriseStage") or payload.get("redwoodStage") or "")
        if work.startswith("enterprise-ordinary-") and stage:
            ordinary_stage[work].add(stage)
            when = _parse_time(event.get("world_at"))
            if when:
                ordinary_times[work][stage].append(when)
        if work.startswith("enterprise-ordinary-") and stage == "decision":
            ordinary_decisions.add(work)
    for stage_map in ordinary_times.values():
        if "task" not in stage_map:
            continue
        ordinary_complete += 1
        ordered = True
        if "decision" in stage_map:
            ordered = ordered and max(stage_map["decision"]) < min(stage_map["task"])
        if "merge_request" in stage_map:
            ordered = ordered and max(stage_map["task"]) < min(stage_map["merge_request"])
        if "deployment" in stage_map:
            ordered = ordered and max(stage_map["merge_request"]) < min(stage_map["deployment"])
        ordinary_ordered += int(ordered)

    cancelled = {
        str(event.get("entity_id"))
        for event in latest_events
        if event.get("object_type") == "merge_request"
        and (event.get("payload") or {}).get("state") == "closed"
        and str(event.get("external_id", "")).startswith("dsid_")
    }
    cancelled_downstream = [
        link
        for link in links
        if str(link.get("source_entity_id")) in cancelled
        and str(link.get("predicate")) in {"implements", "caused_by", "deploys", "implements_deployment"}
    ]

    external_actions = {
        str((event.get("payload") or {}).get("canonicalDocumentId"))
        for event in latest_events
        if (event.get("payload") or {}).get("redwoodExternalAction")
    }
    external_chains = {
        str((event.get("payload") or {}).get("redwoodExternalChainId"))
        for event in latest_events
        if (event.get("payload") or {}).get("redwoodExternalChainId")
    }
    external_outcomes = collections.Counter()
    for event in latest_events:
        payload = event.get("payload") or {}
        if payload.get("redwoodExternalAction"):
            external_outcomes[
                str(payload.get("redwoodExternalOutcome") or payload.get("redwoodExternalStage") or "unknown")
            ] += 1

    project_links = [link for link in links if link.get("rule_id") == "enterprise_project.depends_on.v1"]
    project_nodes = {str(link.get("source_entity_id")) for link in project_links} | {
        str(link.get("target_entity_id")) for link in project_links
    }
    incoming = collections.Counter(str(link.get("target_entity_id")) for link in project_links)
    roots = project_nodes - set(incoming)
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for link in project_links:
        graph[str(link.get("source_entity_id"))].add(str(link.get("target_entity_id")))
    depths: dict[str, int] = {}

    def depth(node: str, seen: set[str] | None = None) -> int:
        if node in depths:
            return depths[node]
        seen = seen or set()
        if node in seen:
            return 0
        children = graph.get(node, set())
        depths[node] = 1 + max((depth(child, seen | {node}) for child in children), default=0)
        return depths[node]

    max_depth = max((depth(node) for node in project_nodes), default=0)

    task_events = [
        event
        for event in latest_events
        if event.get("source") == "jira-redwood"
        and (event.get("payload") or {}).get("fields", {}).get("issuetype", {}).get("name") == "Task"
    ]
    task_texts = [
        str((event.get("payload") or {}).get("fields", {}).get("description") or "") for event in task_events
    ]
    task_sigs = collections.Counter(_semantic_signature(text) for text in task_texts)
    project_events = [
        event
        for event in latest_events
        if event.get("source") == "confluence-redwood"
        and "enterprise-project-" in str(event.get("external_id", ""))
    ]
    decision_events = [
        event
        for event in latest_events
        if event.get("source") == "confluence-redwood"
        and "enterprise-decision-" in str(event.get("external_id", ""))
    ]
    project_sigs = collections.Counter(_semantic_signature(_payload_text(event)) for event in project_events)
    decision_sigs = collections.Counter(
        _semantic_signature(_payload_text(event)) for event in decision_events
    )
    messages = [event for event in latest_events if event.get("source") == "mail-redwood"]
    meetings = [event for event in latest_events if event.get("object_type") == "transcript"]
    subjects = [str((event.get("payload") or {}).get("subject") or "") for event in messages]
    bodies = [
        str(((event.get("payload") or {}).get("body") or {}).get("content") or "") for event in messages
    ]
    comments = [
        comment.get("body", "")
        for event in latest_events
        for comment in (
            (event.get("payload") or {}).get("fields", {}).get("comment", {}).get("comments", []) or []
        )
        if isinstance(comment, dict)
    ]
    mrs = [
        event
        for event in latest_events
        if event.get("object_type") == "merge_request"
        and str(event.get("external_id", "")).startswith("dsid_")
    ]
    mr_fields = {
        field: _distribution([len((event.get("payload") or {}).get(field) or []) for event in mrs])
        for field in ("reviewers", "approvals", "commits", "changed_files", "pipelines", "review_comments")
    }
    changed_patterns = {
        re.sub(r"\b(?:[a-f0-9]{7,40}|\d+)\b", "<id>", str(path).casefold())
        for event in mrs
        for path in ((event.get("payload") or {}).get("changed_files") or [])
    }
    changed_sizes = [len((event.get("payload") or {}).get("changed_files") or []) for event in mrs]

    causal_direction_violations = 0
    causal_rule_prefixes = (
        "enterprise_batch.",
        "external_ticket.",
        "external_meeting.",
        "external_decision.",
        "external_task.",
        "external_mr.",
        "external_deployment.",
        "external_incident.",
        "external_postmortem.",
        "external_chain.",
    )
    for link in links:
        if not str(link.get("rule_id", "")).startswith(causal_rule_prefixes):
            continue
        source = by_reference.get(str(link.get("source_entity_id")))
        target = by_reference.get(str(link.get("target_entity_id")))
        source_time = _parse_time(source.get("world_at")) if source else None
        target_time = _parse_time(target.get("world_at")) if target else None
        if source_time and target_time and source_time <= target_time:
            causal_direction_violations += 1

    routine_types = {
        "message",
        "transcript",
        "issue",
        "merge_request",
        "deal",
        "quote",
        "contract",
        "invoice",
        "payment",
        "amendment",
        "page",
    }
    allowed_stages = {"incident", "mitigation", "emergency_deployment"}
    routine_weekend = collections.Counter()
    routine_off_hours = collections.Counter()
    allowed_off_hours = collections.Counter()
    for event in latest_events:
        when = _parse_time(event.get("world_at"))
        if not when or event.get("object_type") not in routine_types:
            continue
        payload = event.get("payload") or {}
        allowed = (
            str(
                payload.get("redwoodStage")
                or payload.get("redwoodExternalStage")
                or payload.get("redwoodOnCallKind")
                or ""
            )
            in allowed_stages
        )
        if when.weekday() >= 5:
            (allowed_off_hours if allowed else routine_weekend)[str(event.get("object_type"))] += 1
        if not 8 <= when.hour < 18:
            (allowed_off_hours if allowed else routine_off_hours)[str(event.get("object_type"))] += 1

    numeric_cases = [case for case in cases if case.get("category") == "numeric"]
    numeric_families = collections.Counter()
    numeric_nonzero = 0
    for case in numeric_cases:
        case_nonzero = False
        for item in case.get("answer_facts", []):
            key = str(item.get("key", ""))
            values = item.get("any") if isinstance(item.get("any"), list) else [item.get("value")]
            if key in {"financial_family", "family", "scenario_family"}:
                numeric_families[str(next((value for value in values if value is not None), "unknown"))] += 1
            for value in values:
                try:
                    case_nonzero = case_nonzero or float(str(value).replace(",", "")) != 0.0
                except (TypeError, ValueError):
                    continue
        numeric_nonzero += int(case_nonzero)
    case_accounts: collections.Counter[str] = collections.Counter()
    case_families: collections.Counter[str] = collections.Counter()
    for case in cases:
        evidence = [by_reference.get(str(item.get("entity_id"))) for item in case.get("evidence", [])]
        evidence = [event for event in evidence if event]
        payloads = [(event.get("payload") or {}) for event in evidence]
        if any(payload.get("redwoodExternalChainId") for payload in payloads):
            family = "external"
        elif any(event.get("source") == "hubspot-redwood" for event in evidence):
            family = "commercial"
        elif any(
            str(payload.get("redwoodCausalChainId", "")).startswith("enterprise-") for payload in payloads
        ):
            family = "operational"
        else:
            family = "support"
        case_families[family] += 1
        for payload in payloads:
            refs = payload.get("redwoodReferences") or {}
            account = refs.get("accountId") or (payload.get("properties") or {}).get("account_id")
            if account:
                case_accounts[str(account)] += 1
    acl_cases = [case for case in cases if case.get("category") == "acl"]
    acl_scenarios = collections.Counter(str(case.get("acl_scenario")) for case in acl_cases)
    principals = collections.Counter(str(case.get("principal")) for case in cases if case.get("principal"))
    departments = collections.Counter(
        str((event.get("payload") or {}).get("redwoodAclDepartment"))
        for case in acl_cases
        for item in case.get("evidence", [])
        for event in [by_reference.get(str(item.get("entity_id")))]
        if event and (event.get("payload") or {}).get("redwoodAclDepartment")
    )
    op_counts = collections.Counter(str(event.get("op")) for event in events)
    vacation_conflicts = 0
    people_path = root / "config" / "people.yaml"
    if people_path.exists():
        import yaml

        people = yaml.safe_load(people_path.read_text(encoding="utf-8")) or {}
        vacations = [entry for entry in people.get("availability", []) if entry.get("status") == "vacation"]
        for event in latest_events:
            when = _parse_time(event.get("world_at"))
            text = payload_text_by_event[id(event)]
            if not when:
                continue
            for vacation in vacations:
                person_id = str(vacation.get("person_id", "")).casefold()
                account_id = "acct:" + person_id.removeprefix("rw-")
                start = _parse_time(vacation.get("valid_from"))
                end = _parse_time(vacation.get("valid_to"))
                if start and end and start <= when < end and (account_id in text or person_id in text):
                    vacation_conflicts += 1
                    break

    return {
        "enterprise_chronology": chronology(
            "enterprise-causal-batch-",
            ["ticket", "meeting", "task", "merge_request", "deployment", "incident"],
        ),
        "ordinary_chronology": {
            "groups": len(ordinary_stage),
            "complete": ordinary_complete,
            "ordered": ordinary_ordered,
            "violations": ordinary_complete - ordinary_ordered,
        },
        "causal_edge_direction": {"links": len(links), "direction_violations": causal_direction_violations},
        "cancelled_mr_downstream": {
            "cancelled_merge_requests": len(cancelled),
            "downstream_links": len(cancelled_downstream),
        },
        "external_denominator": {
            "source_records": 634,
            "canonical_signals": len(external_actions),
            "external_chains": len(external_chains),
            "reconciled": len(external_actions) == 443,
        },
        "external_outcome_distribution": dict(sorted(external_outcomes.items())),
        "project_dag": {
            "nodes": len(project_nodes),
            "edges": len(project_links),
            "roots": len(roots),
            "max_depth": max_depth,
        },
        "ordinary_decision_linkage": {
            "groups": len(ordinary_stage),
            "with_governing_decision": len(ordinary_decisions),
        },
        "task_semantic_diversity": {
            "count": len(task_texts),
            "unique_normalized": len(task_sigs),
            "uniqueness": round(len(task_sigs) / len(task_texts), 6) if task_texts else 0.0,
            "dominant_ratio": round(max(task_sigs.values(), default=0) / len(task_texts), 6)
            if task_texts
            else 0.0,
        },
        "project_semantic_diversity": {
            "count": len(project_events),
            "unique_normalized": len(project_sigs),
            "uniqueness": round(len(project_sigs) / len(project_events), 6) if project_events else 0.0,
            "dominant_ratio": round(max(project_sigs.values(), default=0) / len(project_events), 6)
            if project_events
            else 0.0,
        },
        "decision_semantic_diversity": {
            "count": len(decision_events),
            "unique_normalized": len(decision_sigs),
            "uniqueness": round(len(decision_sigs) / len(decision_events), 6) if decision_events else 0.0,
            "dominant_ratio": round(max(decision_sigs.values(), default=0) / len(decision_events), 6)
            if decision_events
            else 0.0,
        },
        "text_diversity": {
            "comments": {"count": len(comments), "unique": len(set(comments))},
            "meetings": {
                "count": len(meetings),
                "unique": len({_semantic_signature(_payload_text(event)) for event in meetings}),
            },
            "subjects": {
                "count": len(subjects),
                "unique_normalized": len({_semantic_signature(value) for value in subjects}),
            },
            "bodies": {
                "count": len(bodies),
                "unique_normalized": len({_semantic_signature(value) for value in bodies}),
            },
        },
        "mr_distributions": mr_fields,
        "changed_files": {
            "distribution": _distribution(changed_sizes),
            "patterns": len(changed_patterns),
            "gt4": sum(value > 4 for value in changed_sizes),
            "ge10": sum(value >= 10 for value in changed_sizes),
            "gt4_ratio": round(sum(value > 4 for value in changed_sizes) / len(changed_sizes), 6)
            if changed_sizes
            else 0.0,
            "ge10_ratio": round(sum(value >= 10 for value in changed_sizes) / len(changed_sizes), 6)
            if changed_sizes
            else 0.0,
        },
        "routine_weekend_by_object_type": dict(sorted(routine_weekend.items())),
        "routine_off_hours_by_object_type": dict(sorted(routine_off_hours.items())),
        "allowed_off_hours_by_object_type": dict(sorted(allowed_off_hours.items())),
        "vacation_conflicts": vacation_conflicts,
        "cases": {
            "by_category": dict(
                sorted(collections.Counter(str(case.get("category")) for case in cases).items())
            ),
            "by_family": dict(sorted(case_families.items())),
            "by_account": dict(sorted(case_accounts.items())),
            "by_principal": dict(sorted(principals.items())),
        },
        "acl": {
            "scenarios": dict(sorted(acl_scenarios.items())),
            "departments": dict(sorted(departments.items())),
            "principal_count": len(principals),
        },
        "numeric": {
            "case_count": len(numeric_cases),
            "nonzero": numeric_nonzero,
            "families": dict(sorted(numeric_families.items())),
            "family_count": len(numeric_families),
        },
        "financial_reconciliation": {
            "source_invoice_records": sum(event.get("object_type") == "invoice" for event in latest_events),
            "payments": sum(event.get("object_type") == "payment" for event in latest_events),
            "credit_notes": sum(
                str(event.get("object_type")) == "credit_note"
                or (event.get("payload") or {}).get("properties", {}).get("record_type") == "credit_note"
                for event in latest_events
            ),
            "dashboard_present": (root / "dashboard.json").exists(),
        },
        "mutations_deletions": {
            "operations": dict(sorted(op_counts.items())),
            "revisions": sum(int(event.get("revision", 0)) >= 2 for event in events),
            "deletions": op_counts.get("delete", 0),
        },
        "sample_connectivity": {"events": len(events), "cases": len(cases), "links": len(links)},
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo_root = Path(__file__).resolve().parents[1]
        src_root = repo_root / "src"
        if str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))
        from variator.corpus.acceptance import audit_delivery

        report = audit_delivery(args.root)
        archive_ok, archive_metrics = _archive_audit(args.archive)
        report.setdefault("checks", {})["archive_metadata"] = archive_ok
        report.setdefault("failed", [])
        if not archive_ok:
            report["failed"].append("archive_metadata")
        report["metrics"].update(archive_metrics)
        report["accepted"] = not report["failed"]
        events = _read_gzip_jsonl(args.root / "fixtures" / "timeline.jsonl.gz")
        links = _read_jsonl(args.root / "gold_links.jsonl")
        cases = _read_jsonl(args.root / "cases.jsonl")
        metrics = report.setdefault("metrics", {})
        if isinstance(metrics, dict):
            metrics.update(_raw_metrics(args.root, events, links, cases))
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        report = {
            "accepted": False,
            "checks": {"audit_execution": False},
            "failed": ["audit_execution"],
            "error": f"{type(exc).__name__}: {exc}",
        }

    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.format in {"human", "both"}:
        print(_human(report, args.root))
    if args.format in {"json", "both"}:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("accepted") is True else 1


if __name__ == "__main__":
    sys.exit(main())
