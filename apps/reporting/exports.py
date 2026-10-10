"""
Tabular report exports to Excel (XlsxWriter), CSV and PDF (ReportLab).

Text cells that begin with characters spreadsheet applications interpret as
formulas are prefixed with an apostrophe to prevent CSV/formula injection.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import xlsxwriter
from django.http import HttpResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, TableStyle
from reportlab.platypus import Table as PdfTable

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        # Negative numbers rendered as text are legitimate; everything else is neutralised.
        try:
            Decimal(value.replace(",", ""))
            return value
        except Exception:  # noqa: BLE001
            return "'" + value
    return value


@dataclass
class Column:
    title: str
    kind: str = "text"  # text | money | date | int
    width: int = 18


@dataclass
class Table:
    title: str
    subtitle: str
    columns: list[Column]
    rows: list[list] = field(default_factory=list)
    totals: list | None = None
    notes: list[str] = field(default_factory=list)
    institution: str = ""
    generated_by: str = ""


def _fmt(value, kind):
    if value is None:
        return ""
    if kind == "money" and isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, date | datetime):
        return value.strftime("%d-%b-%Y")
    return str(value)


def to_csv(table: Table) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([safe_text(table.institution)])
    writer.writerow([safe_text(table.title)])
    writer.writerow([safe_text(table.subtitle)])
    writer.writerow([])
    writer.writerow([c.title for c in table.columns])
    for row in table.rows:
        writer.writerow(
            [
                (str(v) if isinstance(v, Decimal) else safe_text(_fmt(v, c.kind)))
                for v, c in zip(row, table.columns, strict=False)
            ]
        )
    if table.totals:
        writer.writerow([str(v) if isinstance(v, Decimal) else safe_text(_fmt(v, "text")) for v in table.totals])
    for note in table.notes:
        writer.writerow([safe_text(note)])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(table: Table) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    ws = wb.add_worksheet(table.title[:31] or "Report")
    title = wb.add_format({"bold": True, "font_size": 14})
    subtitle = wb.add_format({"italic": True, "font_color": "#555555"})
    header = wb.add_format({"bold": True, "bg_color": "#1F3A5F", "font_color": "#FFFFFF", "border": 1})
    money = wb.add_format({"num_format": "#,##0.00;[Red](#,##0.00)", "border": 1})
    money_bold = wb.add_format({"num_format": "#,##0.00;[Red](#,##0.00)", "bold": True, "top": 2})
    text = wb.add_format({"border": 1})
    bold = wb.add_format({"bold": True, "top": 2})
    datef = wb.add_format({"num_format": "dd-mmm-yyyy", "border": 1})
    ws.write(0, 0, table.institution, title)
    ws.write(1, 0, table.title, title)
    ws.write(2, 0, table.subtitle, subtitle)
    ws.write(3, 0, f"Generated {timezone.localtime():%d-%b-%Y %H:%M} by {table.generated_by}", subtitle)
    start = 5
    for col, column in enumerate(table.columns):
        ws.write(start, col, column.title, header)
        ws.set_column(col, col, column.width)
    for r, row in enumerate(table.rows, start=start + 1):
        for c, (value, column) in enumerate(zip(row, table.columns, strict=False)):
            if value is None:
                ws.write_blank(r, c, None, text)
            elif column.kind == "money" and isinstance(value, Decimal):
                ws.write_number(r, c, float(value), money)  # display only; source values are exact decimals
            elif column.kind == "date" and isinstance(value, date):
                ws.write_datetime(r, c, datetime(value.year, value.month, value.day), datef)
            elif column.kind == "int" and isinstance(value, int):
                ws.write_number(r, c, value, text)
            else:
                ws.write_string(r, c, str(safe_text(_fmt(value, column.kind))), text)
    if table.totals:
        r = start + 1 + len(table.rows)
        for c, value in enumerate(table.totals):
            if isinstance(value, Decimal):
                ws.write_number(r, c, float(value), money_bold)
            elif value is not None:
                ws.write_string(r, c, str(value), bold)
    r = start + 3 + len(table.rows)
    for note in table.notes:
        ws.write_string(r, 0, str(safe_text(note)), subtitle)
        r += 1
    ws.freeze_panes(start + 1, 0)
    wb.close()
    return buf.getvalue()


def to_pdf(table: Table) -> bytes:
    buf = io.BytesIO()
    wide = len(table.columns) > 5
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4) if wide else A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=table.title,
    )
    styles = getSampleStyleSheet()
    cell = styles["BodyText"].clone("cell", fontSize=7.5, leading=9)
    story = [
        Paragraph(table.institution, styles["Title"]),
        Paragraph(table.title, styles["Heading2"]),
        Paragraph(table.subtitle, styles["Italic"]),
        Paragraph(f"Generated {timezone.localtime():%d-%b-%Y %H:%M} by {table.generated_by}", styles["Italic"]),
        Spacer(1, 4 * mm),
    ]
    data = [[Paragraph(f"<b>{c.title}</b>", cell) for c in table.columns]]
    for row in table.rows:
        data.append([Paragraph(_escape(_fmt(v, c.kind)), cell) for v, c in zip(row, table.columns, strict=False)])
    if table.totals:
        data.append([Paragraph(f"<b>{_escape(_fmt(v, 'money'))}</b>", cell) for v in table.totals])
    pdf_table = PdfTable(data, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    for idx, column in enumerate(table.columns):
        if column.kind == "money":
            style.append(("ALIGN", (idx, 1), (idx, -1), "RIGHT"))
    pdf_table.setStyle(TableStyle(style))
    story.append(pdf_table)
    for note in table.notes:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(_escape(note), styles["Italic"]))
    doc.build(story)
    return buf.getvalue()


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CONTENT_TYPES = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", to_xlsx),
    "csv": ("text/csv; charset=utf-8", to_csv),
    "pdf": ("application/pdf", to_pdf),
}


def export_response(table: Table, fmt: str, filename: str) -> HttpResponse:
    content_type, func = CONTENT_TYPES[fmt]
    response = HttpResponse(func(table), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}.{fmt}"'
    return response
