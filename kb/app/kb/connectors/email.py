"""Corporate mail adapter: Microsoft Graph-like message JSON -> one Email record + one document per attachment.

Envelope: object_type "message", external_id = the payload's internetMessageId, container None, acl = the
participants (principals/groups) computed by the connector's mailbox sync. The adapter never widens or invents
an ACL: records carry exactly the envelope ACL (None -> the journal default, i.e. visible to admins only).

Payload (GET /users/{id}/messages/{id}?$expand=attachments, plus text extracted by the connector):
  id, internetMessageId, conversationId, subject, from/sender.emailAddress{name,address}, toRecipients[],
  ccRecipients[], sentDateTime, receivedDateTime, inReplyTo, references[], importance, body{contentType,content},
  hasAttachments, attachments[{id, name, contentType, size, isInline, extractedText,
                               documentMeta{docType, title, number, date, counterparty, amount, currency}}].

Entity ids:
  mail.<payload id>              message      e.g. mail.msg-000123
  mail.<payload id>.<att id>     attachment   external_id "<internetMessageId>#<att id>", kind from docType
Accounts of an email source are e-mail addresses, lower-cased (bootstrap maps people.yaml accounts the same way).

Declared links:
  inReplyTo (else the last of references)  -> replies_to (Email -> parent Email; late binding if the parent
                                              arrives later)
  attachment                                -> attached_to statement (document -> its message)
  documentMeta.counterparty (organization id) -> counterparty statement -> organization of the directory source
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from kb.connectors._util import (
    Links, account, aliases, clean_ws, evidence, iso, items, parse_dt, record_acl, string,
)
from kb.connectors.base import AdapterContext, SourceConfig, register
from kb.contracts import CanonicalRecord, Ref, SourceEnvelope

DOC_KINDS = {
    "contract": "Contract", "amendment": "Amendment", "invoice": "Invoice", "act": "Act",
    "proposal": "CommercialProposal", "claim": "Claim", "letter": "Letter", "specification": "Specification",
    "price_list": "PriceList", "order": "Order", "nda": "NDA", "report": "Report", "other": "BusinessDocument",
}
DEFAULT_DOC_KIND = "BusinessDocument"

_URL = re.compile(r"https?://[^\s\]\)\|>\"'<]+")
_UNSAFE = re.compile(r"[^0-9A-Za-z_.@-]+")


def slug(value: str) -> str:
    """A readable entity-id segment: "<20260616.0931@example.com>" -> "20260616.0931@example.com"."""
    s = _UNSAFE.sub("-", str(value).strip().strip("<>")).strip("-.")
    return s or "unknown"


def message_entity_id(payload_id: str) -> str:
    return f"mail.{slug(payload_id)}"


def message_ref(source_id: str, internet_message_id: str, label: str | None = None) -> Ref:
    return Ref(source_id=source_id, object_type="message", external_id=internet_message_id, label=label)


def _address(obj: Any) -> tuple[str | None, str | None]:
    """(address lower-cased, display name) of a Graph recipient {"emailAddress": {...}}, a bare {...} or a string."""
    if isinstance(obj, str):
        return (obj.strip().lower() or None), None
    if not isinstance(obj, dict):
        return None, None
    ea = obj.get("emailAddress") if isinstance(obj.get("emailAddress"), dict) else obj
    addr = string(ea.get("address"))
    name = string(ea.get("name"))
    return (addr.strip().lower() if addr and addr.strip() else None), (" ".join(name.split()) if name else None)


def _recipients(value: Any) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for r in items(value):
        addr, name = _address(r)
        if addr and addr not in seen:
            seen.add(addr)
            out.append((addr, name))
    return out


def _who(addr: str, name: str | None) -> str:
    return f"{name} <{addr}>" if name and name != addr else addr


def html_to_text(text: str) -> str:
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)<li\b[^>]*>", "\n- ", t)
    t = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def _body_text(body: Any) -> str:
    if isinstance(body, str):
        return clean_ws(body)
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if not isinstance(content, str):
        return ""
    if str(body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)
    return clean_ws(content)


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    off = dt.strftime("%z")
    return dt.strftime("%Y-%m-%d %H:%M") + (f" {off[:3]}:{off[3:]}" if off else "")


def _amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _date(value: Any) -> str | None:
    d = parse_dt(value)
    if d is not None:
        return d.date().isoformat()
    return string(value)


def _directory_source(ctx: AdapterContext) -> str | None:
    dirs = ctx.tenant_sources.by_provider("directory") if ctx.tenant_sources else []
    return dirs[0].id if dirs else None


class EmailAdapter:
    provider = "email"

    def normalize(self, env: SourceEnvelope, ctx: AdapterContext) -> list[CanonicalRecord]:
        mid = str(env.external_id)
        key = message_ref(env.source_id, mid)
        p = env.payload if isinstance(env.payload, dict) else {}
        entity_id = message_entity_id(string(p.get("id")) or mid)
        if env.operation == "delete" or env.payload is None:
            return [CanonicalRecord(key=key, entity_id=entity_id, kind="Email",
                                    title=string(p.get("subject")) or mid, deleted=True)]

        subject = " ".join(str(p.get("subject") or "").split()) or "(no subject)"
        sender, sender_name = _address(p.get("from"))
        if sender is None:
            sender, sender_name = _address(p.get("sender"))
        to = _recipients(p.get("toRecipients"))
        cc = _recipients(p.get("ccRecipients"))
        sent = parse_dt(p.get("sentDateTime")) or parse_dt(p.get("receivedDateTime")) or env.source_modified_at
        thread = string(p.get("conversationId"))
        refs = [r for r in items(p.get("references")) if isinstance(r, str) and r.strip()]
        parent = (string(p.get("inReplyTo")) or (refs[-1] if refs else None))
        parent = parent.strip() if parent and parent.strip() and parent.strip() != mid else None
        acl = record_acl(env, ctx, None)
        author = account(ctx, sender, sender_name) if sender else None
        text = _body_text(p.get("body"))

        links = Links(key)
        links.statement("author", author)
        for addr, name in to:
            links.statement("addressed_to", account(ctx, addr, name))
        for addr, name in cc:
            links.statement("cc_to", account(ctx, addr, name))
        links.statement("in_thread", literal=thread)
        if parent:
            links.cause("replies_to", message_ref(env.source_id, parent),
                        evidence=evidence(f"In-Reply-To: {parent}", field="inReplyTo"))
        for u in _URL.findall(text):
            links.statement("mentions", ctx.ref_from_url(u.rstrip(".,;")))

        children: list[CanonicalRecord] = []
        attachment_lines: list[str] = []
        att_names: list[str] = []
        directory = _directory_source(ctx)
        seen_ids: set[str] = set()
        for n, a in enumerate(items(p.get("attachments")), 1):
            if not isinstance(a, dict):
                continue
            meta = a.get("documentMeta") if isinstance(a.get("documentMeta"), dict) else {}
            extracted = clean_ws(a.get("extractedText")) if isinstance(a.get("extractedText"), str) else ""
            if a.get("isInline") and not meta and not extracted:
                continue  # signature logos and inline images are not documents
            raw_id = a.get("id")
            att_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) and str(raw_id).strip() else f"att-{n}"
            if att_id in seen_ids:
                att_id = f"{att_id}-{n}"
            seen_ids.add(att_id)
            filename = string(a.get("name")) or att_id
            title = " ".join(str(meta.get("title") or "").split()) or filename
            doc_type = str(meta.get("docType") or "other").strip().lower() or "other"
            kind = DOC_KINDS.get(doc_type, DEFAULT_DOC_KIND)
            number = string(meta.get("number"))
            counterparty = string(meta.get("counterparty"))
            size = a.get("size")
            att_names.append(filename)
            attachment_lines.append(f"- {filename}" + (f" ({title})" if title != filename else ""))

            akey = Ref(source_id=env.source_id, object_type="attachment", external_id=f"{mid}#{att_id}")
            alinks = Links(akey)
            alinks.statement("attached_to", message_ref(env.source_id, mid, subject),
                             evidence=evidence(f"{filename} — attachment of the email “{subject}”", field="attachments",
                                               id=att_id))
            if counterparty and directory:
                alinks.statement("counterparty",
                                 Ref(source_id=directory, object_type="organization", external_id=counterparty,
                                     label=counterparty),
                                 evidence=evidence(f"counterparty: {counterparty}", field="documentMeta.counterparty"))
            children.append(CanonicalRecord(
                key=akey,
                entity_id=f"{entity_id}.{slug(att_id)}",
                kind=kind,
                title=title,
                fields={
                    "doc_type": doc_type,
                    "number": number,
                    "doc_date": _date(meta.get("date")),
                    "counterparty": counterparty,
                    "amount": _amount(meta.get("amount")),
                    "currency": string(meta.get("currency")),
                    "filename": filename,
                    "content_type": string(a.get("contentType")),
                    "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
                },
                body=extracted or f"# {title}\n\nFile: {filename}",
                valid_from=sent,
                actor=author,
                aliases=aliases((number, "key"), (title, "title"), (filename, "label")),
                statements=alinks.statements,
                acl=acl,
            ))

        head = [f"From: {_who(sender, sender_name)}" if sender else "From: (unknown)"]
        if to:
            head.append("To: " + ", ".join(_who(a, nm) for a, nm in to))
        if cc:
            head.append("Cc: " + ", ".join(_who(a, nm) for a, nm in cc))
        if sent:
            head.append(f"Date: {_fmt_dt(sent)}")
        head.append(f"Subject: {subject}")
        parts = [f"# {subject}", "\n".join(head)]
        if text:
            parts.append(text)
        if attachment_lines:
            parts.append("## Attachments\n\n" + "\n".join(attachment_lines))

        has_attachments = p.get("hasAttachments")
        primary = CanonicalRecord(
            key=key,
            entity_id=entity_id,
            kind="Email",
            title=subject,
            fields={
                "subject": subject,
                "from": sender,
                "from_name": sender_name,
                "to": [a for a, _ in to],
                "cc": [a for a, _ in cc],
                "sent_at": iso(sent),
                "thread_id": thread,
                "importance": string(p.get("importance")),
                "has_attachments": bool(has_attachments) if isinstance(has_attachments, bool) else bool(att_names),
                "message_id": mid,
                "in_reply_to": parent,
                "attachments": att_names,
            },
            body="\n\n".join(parts),
            valid_from=sent,
            actor=author,
            aliases=aliases((subject, "title")),
            statements=links.statements,
            causes=links.causes,
            acl=acl,
        )
        return [primary, *children]

    def ref_from_url(self, url: str, source: SourceConfig) -> Ref | None:
        return None  # mail has no browsable base URL


register(EmailAdapter())
