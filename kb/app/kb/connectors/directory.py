"""Directory adapter: the HR roster and the address book (config/people.yaml) become Person and Organization entities.

object_type "person"        employee                -> Person        person.<employee-id-lowercase>
object_type "organization"  client, vendor, bank... -> Organization  org.<id without the "org-" prefix>
                                                                      (org-translog -> org.translog; an id without
                                                                      the prefix is kept as is: acme -> org.acme)
object_type "contact"       external person         -> Person        person.<contact-id-lowercase>, external: true,
                                                                      works_at -> its organization
"""
from __future__ import annotations

import re
from typing import Any

from kb.connectors.base import AdapterContext, SourceConfig, register
from kb.contracts import AliasSpec, CanonicalRecord, Ref, SourceEnvelope, StatementSpec

ORG_KIND_LABELS = {
    "client": "client", "prospect": "prospective client", "vendor": "vendor", "partner": "partner",
    "bank": "bank", "landlord": "landlord", "state": "government body", "other": "organization",
}
_LEGAL_FORM = re.compile(r"^(llc|ltd|inc|gmbh)\b\.?\s*", re.IGNORECASE)
_LEGAL_SUFFIX = re.compile(r"[,\s]+(llc|ltd|inc|gmbh|corp|plc)\.?$", re.IGNORECASE)


def person_entity_id(employee_id: str) -> str:
    return f"person.{employee_id.lower()}"


def contact_entity_id(contact_id: str) -> str:
    return person_entity_id(contact_id)


def organization_entity_id(org_id: str) -> str:
    s = str(org_id)
    return f"org.{s[4:] if s.lower().startswith('org-') and len(s) > 4 else s}"


def short_org_name(name: str) -> str | None:
    """"TransLogistics LLC" -> "TransLogistics": what people actually write."""
    s = _LEGAL_SUFFIX.sub("", _LEGAL_FORM.sub("", name.strip()))
    s = s.strip(" «»\"“”„'")
    return s if s and s != name.strip() else None


def _account_aliases(p: dict[str, Any], skip: set[str]) -> list[AliasSpec]:
    out = []
    for acc in (p.get("accounts") or {}).values():
        a = str(acc) if acc else ""
        if a and not a.startswith("712020:") and a.lower() not in skip:
            out.append(AliasSpec(alias=a, kind="username"))
            skip.add(a.lower())
    return out


class DirectoryAdapter:
    provider = "directory"

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        if env.object_type == "organization":
            return self._organization(env, ctx)
        if env.object_type == "contact":
            return self._contact(env, ctx)
        return self._person(env, ctx)

    @staticmethod
    def _acl(env: SourceEnvelope, ctx: AdapterContext) -> list[str]:
        return ctx.container_acl(env.container) or ctx.container_acl("people")

    def _person(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        key = Ref(source_id=env.source_id, object_type="person", external_id=env.external_id)
        if env.operation == "delete" or env.payload is None:
            return [CanonicalRecord(key=key, entity_id=person_entity_id(env.external_id), kind="Person",
                                    title=env.external_id, deleted=True)]
        p = env.payload
        aliases = [AliasSpec(alias=p["name"], kind="name"), AliasSpec(alias=p["id"], kind="key")]
        if p.get("email"):
            aliases.append(AliasSpec(alias=p["email"], kind="email"))
        for acc in (p.get("accounts") or {}).values():
            if acc and not str(acc).startswith("712020:"):
                aliases.append(AliasSpec(alias=str(acc), kind="username"))
        body = f"{p['name']} — {p.get('role', '')}, {p.get('department', '')}."
        return [
            CanonicalRecord(
                key=key,
                entity_id=person_entity_id(p["id"]),
                kind="Person",
                title=p["name"],
                fields={k: p.get(k) for k in ("role", "department", "email", "principal") if p.get(k)} | {"employee_id": p["id"]},
                body=body,
                aliases=aliases,
                acl=ctx.container_acl("people"),
            )
        ]

    def _organization(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        key = Ref(source_id=env.source_id, object_type="organization", external_id=env.external_id)
        entity_id = organization_entity_id(env.external_id)
        if env.operation == "delete" or env.payload is None:
            return [CanonicalRecord(key=key, entity_id=entity_id, kind="Organization", title=env.external_id,
                                    deleted=True)]
        p = env.payload
        name = " ".join(str(p.get("name") or p["id"]).split())
        aliases = [AliasSpec(alias=name, kind="name"), AliasSpec(alias=str(p["id"]), kind="key")]
        short = short_org_name(name)
        if short:
            aliases.append(AliasSpec(alias=short, kind="label"))
        if p.get("domain"):
            aliases.append(AliasSpec(alias=str(p["domain"]), kind="key"))
        if p.get("inn"):
            aliases.append(AliasSpec(alias=str(p["inn"]), kind="key"))
        org_kind = str(p.get("kind") or "other")
        facts = [ORG_KIND_LABELS.get(org_kind, org_kind)]
        if p.get("domain"):
            facts.append(f"domain {p['domain']}")
        if p.get("inn"):
            facts.append(f"tax ID {p['inn']}")
        body = f"{name} — " + ", ".join(facts) + "."
        if p.get("description"):
            body += f"\n\n{' '.join(str(p['description']).split())}"
        fields = {"org_id": str(p["id"]), "kind": org_kind, "domain": p.get("domain"), "inn": p.get("inn")}
        if p.get("description"):
            fields["description"] = p["description"]
        return [CanonicalRecord(key=key, entity_id=entity_id, kind="Organization", title=name, fields=fields,
                                body=body, aliases=aliases, acl=self._acl(env, ctx))]

    def _contact(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        key = Ref(source_id=env.source_id, object_type="contact", external_id=env.external_id)
        entity_id = contact_entity_id(env.external_id)
        if env.operation == "delete" or env.payload is None:
            return [CanonicalRecord(key=key, entity_id=entity_id, kind="Person", title=env.external_id, deleted=True)]
        p = env.payload
        name = " ".join(str(p.get("name") or p["id"]).split())
        aliases = [AliasSpec(alias=name, kind="name"), AliasSpec(alias=str(p["id"]), kind="key")]
        seen: set[str] = set()
        if p.get("email"):
            aliases.append(AliasSpec(alias=str(p["email"]), kind="email"))
            seen.add(str(p["email"]).lower())
        aliases += _account_aliases(p, seen)
        org = str(p["org"]) if p.get("org") else None
        org_name = p.get("org_name")
        statements = []
        if org:
            statements.append(StatementSpec(
                predicate="works_at",
                object=Ref(source_id=env.source_id, object_type="organization", external_id=org, label=org_name or org),
            ))
        where = org_name or org
        body = f"{name} — {p.get('role') or 'contact'}" + (f", {where}" if where else "") + " (external contact)."
        fields = {"contact_id": str(p["id"]), "role": p.get("role"), "email": p.get("email"), "external": True,
                  "org": org}
        if org_name:
            fields["org_name"] = org_name
        return [CanonicalRecord(key=key, entity_id=entity_id, kind="Person", title=name, fields=fields, body=body,
                                aliases=aliases, statements=statements, acl=self._acl(env, ctx))]

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        return None


register(DirectoryAdapter())
