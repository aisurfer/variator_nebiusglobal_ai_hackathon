"""The English delivery fixture tests/fixtures/sample-en (contract plancy-kb-corpus/1), read without a database.

It is VARIATOR's orgsim sample (Jira issues, a GitLab merge request, a mail thread) plus lines of the simulation
provider `sim`: every structured object type (contract, invoice, payment, bank_entry, deal, employee_event,
requisition, payroll, incident, deploy, support_ticket, cloud_bill, tax_payment, decision), the cause catalog
(hypothesis, life_event, habit, rule, process) and at least one cause of each type. It was written with VARIATOR's
own code (`variator.orgsim.sample.build_sample`, `Event.to_timeline_line`, `delivery.write_delivery`, anchor
2026-10-26, seed 7); kb/ does not import `variator`, so the fixture is kept as files.

The timeline is kept unpacked (fixtures/timeline.jsonl): the bytes of a gzip file look like Cyrillic letters to
`make check-english`. `materialize(dir)` writes the delivery as the contract wants it (fixtures/timeline.jsonl.gz,
byte for byte the file of the manifest), e.g. to run `python -m kbsim.intake validate` on it:

    cd kb && PYTHONPATH=app:sim:. python -c "from tests.unit.connectors.sample_en import materialize; \
        print(materialize('/tmp/sample-en'))"
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from kb.connectors.base import AdapterContext, TenantSources, get_adapter, load_sources
from kb.contracts import CanonicalRecord, SourceEnvelope

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "sample-en"
TIMELINE = "fixtures/timeline.jsonl.gz"
SIM = "sim"
DIRECTORY = "directory-wp3sample"


@lru_cache
def sources() -> TenantSources:
    return load_sources(FIXTURE / "config" / "sources.yaml")


@lru_cache
def lines() -> tuple[dict[str, Any], ...]:
    text = (FIXTURE / "fixtures" / "timeline.jsonl").read_text(encoding="utf-8")
    return tuple(json.loads(x) for x in text.splitlines() if x.strip())


def materialize(dest: str | Path) -> Path:
    """Copy the fixture to `dest` as a delivery of the contract (timeline gzipped as VARIATOR's writer does it)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURE, dest)
    plain = dest / "fixtures" / "timeline.jsonl"
    raw = plain.read_bytes()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    (dest / TIMELINE).write_bytes(buf.getvalue())
    plain.unlink()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != manifest["files"][TIMELINE]["sha256_unpacked"]:
        raise ValueError("the fixture timeline differs from its manifest")
    sha = hashlib.sha256(buf.getvalue()).hexdigest()
    if manifest["files_sha256"][TIMELINE] != sha:  # another zlib build packs the same text into other bytes
        manifest["files_sha256"][TIMELINE] = sha
        (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def envelope(i: int, ln: dict[str, Any]) -> SourceEnvelope:
    """The envelope the seed/replay path builds from a timeline line."""
    return SourceEnvelope(
        tenant_id=sources().tenant_id, source_id=ln["source"], delivery_id=f"sample-en:{i}",
        operation=ln.get("op", "upsert"), object_type=ln["object_type"], external_id=str(ln["external_id"]),
        container=ln.get("container"), provider_version=str(ln["version"]) if ln.get("version") is not None else None,
        source_modified_at=datetime.fromisoformat(ln.get("modified_at") or ln["at"]),
        observed_at=datetime.fromisoformat(ln["at"]), payload=ln.get("payload"), acl=ln.get("acl"))


def ctx(source_id: str) -> AdapterContext:
    ts = sources()
    return AdapterContext(ts.tenant_id, ts.sources[source_id], ts)


def normalize(env: SourceEnvelope) -> list[CanonicalRecord]:
    return get_adapter(sources().sources[env.source_id].provider).normalize(env, ctx(env.source_id))


@lru_cache
def records() -> tuple[tuple[dict[str, Any], tuple[CanonicalRecord, ...]], ...]:
    """(line, records) of every line of the fixture, in timeline order."""
    return tuple((ln, tuple(normalize(envelope(i, ln)))) for i, ln in enumerate(lines()))


def sim_records() -> list[tuple[dict[str, Any], CanonicalRecord]]:
    return [(ln, recs[0]) for ln, recs in records() if ln["source"] == SIM]
