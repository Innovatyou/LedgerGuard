"""
Strict parsing of fixed-layout portfolio extracts (loan and deposit listings).

Columns are recognised by a list of accepted header aliases (case and spacing
insensitive). Files are all-or-nothing: any invalid row rejects the upload with
a list of row errors, so a control schedule is never built from partial data.
"""

import re
from dataclasses import dataclass

from apps.core.exceptions import BusinessRuleError
from apps.core.money import AmountParseError, parse_amount
from apps.imports.normalize import RowError, parse_date
from apps.imports.parsers import FileRejected, inspect_file, read_table


@dataclass
class Column:
    field: str
    aliases: tuple
    kind: str = "text"  # text | amount | int | bool | date
    required: bool = False


def _key(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_TRUE = {"Y", "YES", "TRUE", "1", "DORMANT", "INACTIVE"}


def parse_records(data: bytes, extension: str, columns: list[Column], max_bytes: int, max_rows=500_000):
    try:
        inspect_file(data, extension, max_bytes)
        headers, rows = read_table(data, extension, max_rows=max_rows)
    except FileRejected as exc:
        raise BusinessRuleError(str(exc)) from exc
    lookup = {_key(h): h for h in headers}
    mapping, missing = {}, []
    for col in columns:
        header = next((lookup[_key(a)] for a in (col.field, *col.aliases) if _key(a) in lookup), None)
        if header is None and col.required:
            missing.append(f"{col.field} (accepted headers: {', '.join(col.aliases)})")
        mapping[col.field] = header
    if missing:
        raise BusinessRuleError("Missing required columns: " + "; ".join(missing))
    records, errors = [], []
    for row_number, raw in rows:
        record, row_errors = {"row_number": row_number}, []
        for col in columns:
            text = raw.get(mapping[col.field]) if mapping[col.field] else None
            text = "" if text is None else str(text).strip()
            if not text:
                if col.required:
                    row_errors.append(f"{col.field} is required")
                record[col.field] = None if col.kind != "text" else ""
                continue
            try:
                if col.kind == "amount":
                    record[col.field] = parse_amount(text)[0]
                elif col.kind == "int":
                    record[col.field] = int(float(text.replace(",", "")))
                elif col.kind == "bool":
                    record[col.field] = text.upper() in _TRUE
                elif col.kind == "date":
                    record[col.field] = parse_date(text)
                else:
                    record[col.field] = text
            except (AmountParseError, RowError, ValueError) as exc:
                row_errors.append(f"{col.field}: {exc}")
        if row_errors:
            errors.append(f"Row {row_number}: {'; '.join(row_errors)}")
        records.append(record)
    if errors:
        shown = errors[:50]
        more = f" … and {len(errors) - 50} more" if len(errors) > 50 else ""
        raise BusinessRuleError("The file has invalid rows and was not loaded: " + " | ".join(shown) + more)
    if not records:
        raise BusinessRuleError("The file contains no data rows.")
    return records
