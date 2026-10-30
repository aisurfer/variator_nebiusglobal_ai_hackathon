import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from variator.adapters import (
    RemoteSourceError,
    constants,
    parse_google_url,
    read_csv,
    read_json,
    read_source,
    read_url,
)

ROOT = Path(__file__).resolve().parents[2]


def test_csv_demo_file():
    ds = read_source(ROOT / "data" / "demo" / "warehouse" / "monthly_orders.csv")
    summary = ds.summary()
    assert summary["kind"] == "table" and summary["rows"] > 100
    assert "orders" in summary["numeric"] and "region" in summary["text_columns"]
    assert 0 < summary["numeric"]["orders"]["q"] <= 1


def test_csv_locale_numbers_and_json_shapes():
    ds = read_csv("item;price\na;1.234,5\nb;2.000,0\n", name="t")
    assert ds.columns["price"].tolist() == [1234.5, 2000.0]
    assert read_json('[{"a": 1, "b": "x"}, {"a": 2}]', name="r").columns["a"].tolist() == [1.0, 2.0]
    assert read_json({"a": [1, 2], "b": [3, 4]}, name="c").rows == 2
    consts = read_json({"rate": 45.5, "team": 8}, name="k")
    assert consts.kind == "constants" and consts.columns["rate"][0] == 45.5
    assert constants({"x": 1}).numeric()["x"].tolist() == [1.0]


def test_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["month", "hours"])
    sheet.append(["2026-01", 120])
    sheet.append(["2026-02", 130])
    path = tmp_path / "t.xlsx"
    book.save(path)
    assert read_source(path).columns["hours"].tolist() == [120.0, 130.0]


def _pdf(path: Path) -> None:
    pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table

    rows = [
        ["month", "rate", "hours"],
        ["2026-01", "45.5", "1200"],
        ["2026-02", "47", "1300"],
        ["2026-03", "46.2", "1250"],
    ]
    SimpleDocTemplate(str(path), pagesize=A4).build(
        [
            Paragraph("Vendor report. Blended rate rose 3% in Q1.", getSampleStyleSheet()["Normal"]),
            Table(rows),
        ]
    )


def test_pdf_text_and_table(tmp_path):
    pytest.importorskip("pdfplumber")
    path = tmp_path / "report.pdf"
    _pdf(path)
    ds = read_source(path)
    assert ds.kind == "document" and "Blended rate" in ds.text
    assert ds.columns["hours"].tolist() == [1200.0, 1300.0, 1250.0]
    assert ds.summary()["text_chars"] > 20


def test_google_url_parsing():
    ref = parse_google_url("https://docs.google.com/spreadsheets/d/1AbC-x_9/edit#gid=456")
    assert (ref.kind, ref.file_id, ref.gid) == ("sheet", "1AbC-x_9", "456")
    assert parse_google_url("https://docs.google.com/document/d/DOC1/edit").kind == "doc"
    assert parse_google_url("https://docs.google.com/presentation/d/S1/edit").kind == "slides"
    assert parse_google_url("https://example.com/a.csv") is None


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_google_sheet_public_and_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="a,b\n1,2\n3,4\n", headers={"content-type": "text/csv"})

    ds = read_url("https://docs.google.com/spreadsheets/d/ID1/edit#gid=7", client=_client(handler))
    assert ds.columns["b"].tolist() == [2.0, 4.0] and ds.source == "google:sheet:ID1"
    assert "export?format=csv&gid=7" in str(seen[-1].url)
    read_url("https://docs.google.com/spreadsheets/d/ID1/edit", client=_client(handler), token="tok")
    assert seen[-1].headers[
        "authorization"
    ] == "Bearer tok" and "googleapis.com/drive/v3/files/ID1/export" in str(seen[-1].url)


def test_google_doc_and_slides(tmp_path):
    pytest.importorskip("pdfplumber")
    pdf = tmp_path / "s.pdf"
    _pdf(pdf)

    def handler(request: httpx.Request) -> httpx.Response:
        if "document" in str(request.url):
            return httpx.Response(200, text="Rates rose 3%.", headers={"content-type": "text/plain"})
        return httpx.Response(200, content=pdf.read_bytes(), headers={"content-type": "application/pdf"})

    doc = read_url("https://docs.google.com/document/d/D/edit", client=_client(handler))
    assert doc.kind == "document" and doc.text == "Rates rose 3%."
    slides = read_url("https://docs.google.com/presentation/d/S/edit", client=_client(handler))
    assert slides.kind == "document" and slides.source == "google:slides:S"


def test_private_google_file_gives_clear_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"})

    with pytest.raises(RemoteSourceError, match="anyone with the link"):
        read_url("https://docs.google.com/spreadsheets/d/X/edit", client=_client(handler))
    with pytest.raises(RemoteSourceError, match="access denied"):
        read_url("https://example.com/a.csv", client=_client(lambda r: httpx.Response(403)))


def test_plain_url_json():
    body = json.dumps([{"x": 1}, {"x": 2}])
    ds = read_url("https://example.com/data.json", client=_client(lambda r: httpx.Response(200, text=body)))
    assert np.nanmean(ds.columns["x"]) == 1.5
