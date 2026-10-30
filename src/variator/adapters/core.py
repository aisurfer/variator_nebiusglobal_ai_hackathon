"""Input adapters: turn files, PDFs, constants and MCP tool output into a ``Dataset``.

The adapter is selected by code (file extension, then content sniffing); no LLM is
needed. A dataset keeps the full columns on the server; tools return only its
``summary()``, which is small.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from variator.ingestion.parsing import clean_cell, parse_numeric
from variator.ingestion.tables import decode_text, sniff_delimiter
from variator.quality import score_series

MAX_ROWS = 200_000
SUMMARY_COLUMNS = 40


@dataclass
class Dataset:
    name: str
    source: str
    kind: str  # table | constants | document
    columns: dict[str, np.ndarray]  # numeric columns as float64 (NaN = missing); text as object
    origin: str = "data"
    notes: list[str] = field(default_factory=list)
    text: str = ""  # document text for fact extraction (PDF); not sent in the summary
    scanned_pages: list[int] = field(default_factory=list)  # 1-based pages without a text layer

    @property
    def rows(self) -> int:
        return max((len(c) for c in self.columns.values()), default=0)

    def numeric(self) -> dict[str, np.ndarray]:
        return {k: v for k, v in self.columns.items() if v.dtype == np.float64}

    def summary(self) -> dict[str, Any]:
        """Compact profile: per numeric column n, min, median, max, Q. Text columns by name only."""
        cols: dict[str, Any] = {}
        for name, values in list(self.numeric().items())[:SUMMARY_COLUMNS]:
            finite = values[np.isfinite(values)]
            q = score_series(values, origin=self.origin)
            cols[name] = {
                "n": int(finite.size),
                "min": _round(finite.min()) if finite.size else None,
                "median": _round(np.median(finite)) if finite.size else None,
                "max": _round(finite.max()) if finite.size else None,
                "q": q.q,
            }
        text = [k for k, v in self.columns.items() if v.dtype != np.float64]
        return {
            "name": self.name,
            "kind": self.kind,
            "rows": self.rows,
            "numeric": cols,
            "text_columns": text[:SUMMARY_COLUMNS],
            "notes": self.notes,
            **({"text_chars": len(self.text)} if self.text else {}),
            **({"scanned_pages": self.scanned_pages} if self.scanned_pages else {}),
        }


def _round(x: float) -> float:
    return float(f"{float(x):.6g}")


def read_source(source: str | Path, *, name: str | None = None, origin: str = "data") -> Dataset:
    """Read a CSV/TSV, JSON, XLSX or PDF file; the format comes from the extension or the content."""
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"no file {str(source)!r}")
    label = name or path.stem
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx(path, name=label, origin=origin)
    raw = path.read_bytes()
    if suffix == ".pdf" or raw[:5] == b"%PDF-":
        return read_pdf(path, name=label, origin=origin)
    text = decode_text(raw)
    if suffix == ".json" or (suffix not in (".csv", ".tsv", ".txt") and text.lstrip()[:1] in "[{"):
        return read_json(text, name=label, source=str(path), origin=origin)
    return read_csv(text, name=label, source=str(path), origin=origin)


def read_csv(text: str, *, name: str, source: str = "inline", origin: str = "data") -> Dataset:
    delimiter, _ = sniff_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError(f"{name}: empty table")
    header = [h.strip() or f"col_{i}" for i, h in enumerate(rows[0])]
    body = rows[1 : MAX_ROWS + 1]
    notes = [f"truncated to {MAX_ROWS} rows"] if len(rows) - 1 > MAX_ROWS else []
    cells = {h: [r[i].strip() if i < len(r) else "" for r in body] for i, h in enumerate(header)}
    return Dataset(name, source, "table", {h: _column(v) for h, v in cells.items()}, origin, notes)


def read_json(text: str | Any, *, name: str, source: str = "inline", origin: str = "data") -> Dataset:
    """A list of records, a mapping of lists (columns) or a mapping of scalars (constants)."""
    data = json.loads(text) if isinstance(text, str) else text
    if isinstance(data, Mapping) and all(not isinstance(v, (list, dict)) for v in data.values()):
        return constants(data, name=name, source=source, origin=origin)
    if isinstance(data, Mapping):
        lists = {k: v for k, v in data.items() if isinstance(v, list)}
        if not lists:
            raise ValueError(f"{name}: JSON mapping has no list columns")
        return Dataset(name, source, "table", {k: _column(v) for k, v in lists.items()}, origin)
    if isinstance(data, list) and data and all(isinstance(r, Mapping) for r in data):
        keys: list[str] = []
        for record in data[:MAX_ROWS]:
            keys.extend(k for k in record if k not in keys)
        return Dataset(
            name, source, "table", {k: _column([r.get(k) for r in data[:MAX_ROWS]]) for k in keys}, origin
        )
    raise ValueError(f"{name}: unsupported JSON shape (use records, columns or constants)")


def read_xlsx(path: Path, *, name: str, sheet: str | None = None, origin: str = "data") -> Dataset:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # optional extra
        raise ImportError("XLSX needs the optional extra: uv sync --extra xlsx") from exc
    book = load_workbook(path, read_only=True, data_only=True)
    ws = book[sheet] if sheet else book.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    book.close()
    if not rows:
        raise ValueError(f"{name}: empty sheet")
    header = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    body = rows[1 : MAX_ROWS + 1]
    columns = {h: _column([r[i] if i < len(r) else None for r in body]) for i, h in enumerate(header)}
    return Dataset(name, str(path), "table", columns, origin)


def read_pdf(path: Path, *, name: str, origin: str = "data", max_pages: int = 200) -> Dataset:
    """Text and tables of a PDF with a text layer; pages without text are listed as scanned.

    All tables with the same header are joined into one table; the largest table group
    becomes the columns. Scanned pages go to the vision reader: see ``pdf_page_png``.
    """
    pdfplumber = _pdfplumber()
    texts: list[str] = []
    scanned: list[int] = []
    groups: dict[tuple[str, ...], list[list[Any]]] = {}
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages[:max_pages]
        for number, page in enumerate(pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if len(page_text) < 20 and not page.extract_words():
                scanned.append(number)
                continue
            texts.append(page_text)
            # Ruled tables first; without lines, columns come from the text positions.
            tables = page.extract_tables() or page.extract_tables(_TEXT_TABLES)
            for table in tables:
                rows = [[(c or "").strip() for c in row] for row in table if row and any(row)]
                if len(rows) < 2:
                    continue
                header = tuple(h or f"col_{i}" for i, h in enumerate(rows[0]))
                groups.setdefault(header, []).extend(rows[1:])
        notes = (
            [f"read the first {max_pages} of {len(pdf.pages)} pages"] if len(pdf.pages) > max_pages else []
        )
    columns: dict[str, np.ndarray] = {}
    if groups:
        header, body = max(groups.items(), key=lambda kv: len(kv[1]))
        columns = {h: _column([r[i] if i < len(r) else None for r in body]) for i, h in enumerate(header)}
        if len(groups) > 1:
            notes.append(f"{len(groups)} table layouts found; the largest one is used as columns")
    if scanned:
        notes.append(f"{len(scanned)} scanned page(s): use the vision reader (Super VL)")
    return Dataset(name, str(path), "document", columns, origin, notes, "\n\n".join(texts), scanned)


_TEXT_TABLES = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def pdf_page_png(path: str | Path, page: int, *, resolution: int = 150) -> bytes:
    """Render one 1-based page to PNG for ``ingestion.vision.facts_from_image``."""
    pdfplumber = _pdfplumber()
    with pdfplumber.open(Path(path)) as pdf:
        image = pdf.pages[page - 1].to_image(resolution=resolution)
        buffer = io.BytesIO()
        image.original.save(buffer, format="PNG")
        return buffer.getvalue()


def _pdfplumber() -> Any:
    try:
        import pdfplumber
    except ImportError as exc:  # optional extra
        raise ImportError("PDF needs the optional extra: uv sync --extra pdf") from exc
    return pdfplumber


def constants(
    values: Mapping[str, Any], *, name: str = "constants", source: str = "inline", origin: str = "data"
) -> Dataset:
    return Dataset(name, source, "constants", {k: _column([v]) for k, v in values.items()}, origin)


def _column(cells: list[Any]) -> np.ndarray:
    """float64 if at least 80% of the non-empty cells are numbers, else an object array."""
    if all(c is None or isinstance(c, (int, float)) for c in cells):
        return np.asarray([np.nan if c is None else float(c) for c in cells], dtype=np.float64)
    texts = [None if c is None else clean_cell(str(c)) for c in cells]
    parsed = parse_numeric(texts, min_share=0.8)
    if parsed is not None:
        return parsed.values
    return np.asarray(["" if c is None else str(c) for c in cells], dtype=object)
