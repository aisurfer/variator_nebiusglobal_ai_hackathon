"""URL adapters: Google Sheets, Docs and Slides, and any HTTP(S) file.

Local code only (``httpx`` + the file adapters); no LLM call. Google files use the
export endpoints:

* Sheets -> CSV of one tab (``gid`` from the URL, default the first tab).
* Docs -> plain text (a ``document`` dataset for fact extraction).
* Slides -> PDF, then ``read_pdf`` (text, tables, scanned slides).

Access: a file shared as "anyone with the link" needs no key. For a private file set
``GOOGLE_ACCESS_TOKEN`` (OAuth token with the drive.readonly scope); then the Drive
API export endpoint is used.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from variator.adapters.core import Dataset, read_csv, read_json, read_source
from variator.ingestion.tables import decode_text

TIMEOUT_S = 60.0
MAX_BYTES = 50 * 1024 * 1024
_GOOGLE_RE = re.compile(r"docs\.google\.com/(spreadsheets|document|presentation)/d/([A-Za-z0-9_-]+)")
_KIND = {"spreadsheets": "sheet", "document": "doc", "presentation": "slides"}
# ASSUMPTION: export formats and Drive API MIME types as documented by Google (not verified live).
_PUBLIC_EXPORT = {
    "sheet": "https://docs.google.com/spreadsheets/d/{id}/export?format=csv{gid}",
    "doc": "https://docs.google.com/document/d/{id}/export?format=txt",
    "slides": "https://docs.google.com/presentation/d/{id}/export/pdf",
}
_DRIVE_EXPORT = "https://www.googleapis.com/drive/v3/files/{id}/export"
_DRIVE_MIME = {"sheet": "text/csv", "doc": "text/plain", "slides": "application/pdf"}


class RemoteSourceError(RuntimeError):
    """The URL cannot be read (no access, not found, too large)."""


@dataclass(frozen=True)
class GoogleRef:
    kind: str  # sheet | doc | slides
    file_id: str
    gid: str | None = None


def parse_google_url(url: str) -> GoogleRef | None:
    match = _GOOGLE_RE.search(url)
    if not match:
        return None
    parsed = urlparse(url)
    gid = parse_qs(parsed.query).get("gid", [None])[0] or parse_qs(parsed.fragment).get("gid", [None])[0]
    return GoogleRef(_KIND[match.group(1)], match.group(2), gid)


def read_url(
    url: str,
    *,
    name: str | None = None,
    origin: str = "data",
    client: httpx.Client | None = None,
    token: str | None = None,
) -> Dataset:
    """Download ``url`` and read it with the matching file adapter."""
    ref = parse_google_url(url)
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        if ref is not None:
            return _read_google(
                ref, http, name=name, origin=origin, token=token or os.environ.get("GOOGLE_ACCESS_TOKEN")
            )
        response = _get(http, url, {})
        label = name or (Path(urlparse(url).path).stem or "download")
        return _from_bytes(response, label, url, origin)
    finally:
        if own:
            http.close()


def _read_google(
    ref: GoogleRef, http: httpx.Client, *, name: str | None, origin: str, token: str | None
) -> Dataset:
    label = name or f"google_{ref.kind}_{ref.file_id[:8]}"
    if token:
        url = _DRIVE_EXPORT.format(id=ref.file_id)
        response = _get(
            http, url, {"Authorization": f"Bearer {token}"}, params={"mimeType": _DRIVE_MIME[ref.kind]}
        )
    else:
        gid = f"&gid={ref.gid}" if ref.gid else ""
        response = _get(http, _PUBLIC_EXPORT[ref.kind].format(id=ref.file_id, gid=gid), {})
    source = f"google:{ref.kind}:{ref.file_id}"
    if "text/html" in response.headers.get("content-type", ""):
        raise RemoteSourceError(
            f"{source}: Google returned a sign-in page; share the file as 'anyone with the link' "
            "or set GOOGLE_ACCESS_TOKEN"
        )
    if ref.kind == "sheet":
        return read_csv(decode_text(response.content), name=label, source=source, origin=origin)
    if ref.kind == "doc":
        text = decode_text(response.content)
        return Dataset(label, source, "document", {}, origin, text=text)
    return _via_file(response.content, ".pdf", label, source, origin)


def _get(
    http: httpx.Client, url: str, headers: dict[str, str], params: dict[str, str] | None = None
) -> httpx.Response:
    try:
        response = http.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise RemoteSourceError(f"{url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise RemoteSourceError(f"{url}: access denied ({response.status_code}); check sharing or the token")
    if response.status_code == 404:
        raise RemoteSourceError(f"{url}: not found")
    if response.status_code >= 400:
        raise RemoteSourceError(f"{url}: HTTP {response.status_code}")
    if len(response.content) > MAX_BYTES:
        raise RemoteSourceError(f"{url}: larger than {MAX_BYTES // (1024 * 1024)} MB")
    return response


def _from_bytes(response: httpx.Response, label: str, url: str, origin: str) -> Dataset:
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    suffix = Path(urlparse(url).path).suffix.lower()
    if ctype == "application/pdf" or suffix == ".pdf" or response.content[:5] == b"%PDF-":
        return _via_file(response.content, ".pdf", label, url, origin)
    if "spreadsheetml" in ctype or suffix in (".xlsx", ".xlsm"):
        return _via_file(response.content, ".xlsx", label, url, origin)
    text = decode_text(response.content)
    if ctype == "application/json" or suffix == ".json" or text.lstrip()[:1] in "[{":
        return read_json(text, name=label, source=url, origin=origin)
    return read_csv(text, name=label, source=url, origin=origin)


def _via_file(data: bytes, suffix: str, label: str, source: str, origin: str) -> Dataset:
    """Binary formats need a file for their readers; the temp file is removed after reading."""
    handle, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        dataset = read_source(path, name=label, origin=origin)
    finally:
        Path(path).unlink(missing_ok=True)
    dataset.source = source
    return dataset
