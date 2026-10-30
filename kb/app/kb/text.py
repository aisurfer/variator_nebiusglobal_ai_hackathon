"""Text helpers shared by the journal, search and extraction: hashing, normalisation, chunking."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import orjson


def canonical_json(value: Any) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS, default=str)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def short_hash(*parts: Any, n: int = 16) -> str:
    return sha256_hex(canonical_json(list(parts)))[:n]


def norm(text: str) -> str:
    """Normalisation for alias lookup: NFKC, casefold, collapse spaces and punctuation."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[\s_]+", " ", t)
    t = re.sub(r"[«»\"“”„'`]+", "", t)
    return t.strip(" .,:;!?")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    section: str | None
    text: str
    char_start: int
    char_end: int
    content_hash: str


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n{2,}")


def _split_long(text: str, start: int, max_chars: int) -> list[tuple[int, int]]:
    """Split [start, start+len) into spans of at most max_chars at sentence/paragraph boundaries."""
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        if n - pos <= max_chars:
            spans.append((start + pos, start + n))
            break
        window = text[pos : pos + max_chars]
        cut = None
        for m in _SENTENCE_END.finditer(window):
            if m.start() >= max_chars // 3:
                cut = m.end()
        if cut is None:
            sp = window.rfind(" ")
            cut = sp + 1 if sp > max_chars // 3 else max_chars
        spans.append((start + pos, start + pos + cut))
        pos += cut
    return spans


def chunk_text(body: str | None, max_chars: int = 900) -> list[Chunk]:
    """Chunk a version body by markdown sections, then by sentences, keeping exact character locators.

    Conditions and negations stay with their sentence because cuts happen only at sentence ends.
    """
    if not body or not body.strip():
        return []
    sections: list[tuple[str | None, int, int]] = []
    heads = list(_HEADING.finditer(body))
    if not heads or heads[0].start() > 0:
        sections.append((None, 0, heads[0].start() if heads else len(body)))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        sections.append((h.group(2).strip(), h.end(), end))
    chunks: list[Chunk] = []
    for section, s, e in sections:
        part = body[s:e]
        lead = len(part) - len(part.lstrip())
        part_stripped = part.strip()
        if not part_stripped:
            continue
        base = s + lead
        for cs, ce in _split_long(part_stripped, base, max_chars):
            raw = body[cs:ce]
            text = raw.strip()
            if not text:
                continue
            offset = raw.find(text)
            cs2, ce2 = cs + offset, cs + offset + len(text)
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    section=section,
                    text=text,
                    char_start=cs2,
                    char_end=ce2,
                    content_hash=sha256_hex(f"{section or ''}\n{text}")[:24],
                )
            )
    return chunks
