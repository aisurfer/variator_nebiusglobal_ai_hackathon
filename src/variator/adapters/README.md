# variator.adapters

Inputs -> `Dataset`. Local code only (0 tokens); an LLM reads the result only on request.

| Need | Use |
|---|---|
| File: CSV, TSV, JSON, XLSX, PDF | `read_source(path)` |
| URL: Google Sheets, Docs, Slides, HTTP | `read_url(url)` (`GOOGLE_ACCESS_TOKEN` for private files) |
| Inline data, MCP tool output | `read_json`, `read_csv`, `constants` |
| Scanned PDF page for Super VL | `pdf_page_png(path, page)` |

Optional extras: `xlsx`, `pdf`.
