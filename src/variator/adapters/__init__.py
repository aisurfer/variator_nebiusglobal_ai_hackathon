"""Input adapters -> ``Dataset``. Local code only; an LLM reads the result only on request.

Files: CSV/TSV, JSON, XLSX, PDF (``read_source``). URLs: Google Sheets/Docs/Slides, any
HTTP(S) file (``read_url``). Inline: ``constants``, ``read_json`` (MCP tool output).
"""

from variator.adapters.core import (
    Dataset,
    constants,
    pdf_page_png,
    read_csv,
    read_json,
    read_pdf,
    read_source,
    read_xlsx,
)
from variator.adapters.remote import GoogleRef, RemoteSourceError, parse_google_url, read_url

__all__ = [
    "Dataset",
    "GoogleRef",
    "RemoteSourceError",
    "constants",
    "parse_google_url",
    "pdf_page_png",
    "read_csv",
    "read_json",
    "read_pdf",
    "read_source",
    "read_url",
    "read_xlsx",
]
