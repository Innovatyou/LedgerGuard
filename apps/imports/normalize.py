"""Row normalisation: dates, amounts, references and narration."""

import hashlib
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from apps.core.money import ZERO, AmountParseError, parse_amount

from .models import AmountMode, Side, TimeFromReference

DEFAULT_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%b-%y",
    "%d/%m/%y",
    "%Y/%m/%d",
    "%d%m%Y",
    "%Y%m%d",
    "%d-%B-%Y",
    "%d %B %Y",
]
_EXCEL_EPOCH = date(1899, 12, 30)
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_TRUE = {"Y", "YES", "TRUE", "1", "REV", "REVERSAL", "R"}


class RowError(ValueError):
    pass


_TIME = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AP]M)?", re.IGNORECASE)
# NIP session ID: 6-digit sending institution code, YYMMDDHHMMSS, 12-digit unique number.
NIP_SESSION_ID = re.compile(
    r"(?<!\d)\d{6}(?P<YY>\d{2})(?P<MO>\d{2})(?P<DD>\d{2})(?P<HH>\d{2})(?P<MM>\d{2})(?P<SS>\d{2})\d{12}(?!\d)"
)


def parse_time(value) -> time | None:
    """Time of day from '14:32', '14:32:05', '2:32 PM' or a timestamp such as '2026-10-17 14:32:05'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, time):
        return value.replace(microsecond=0)
    text = str(value).strip()
    if not text:
        return None
    matches = list(_TIME.finditer(text))
    if not matches:
        raise RowError(f"Unrecognised time {text!r}")
    m = matches[-1]
    hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    meridiem = (m.group(4) or "").upper()
    if meridiem:
        if not 1 <= hour <= 12:
            raise RowError(f"Unrecognised time {text!r}")
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    if hour > 23 or minute > 59 or second > 59:
        raise RowError(f"Unrecognised time {text!r}")
    return time(hour, minute, second)


def time_from_reference(texts, mode: str, pattern: str = "", txn_date: date | None = None) -> time | None:
    """
    Derive the time of day embedded in a reference (e.g. a NIP session ID).

    Only a candidate whose embedded date is valid (and, for NIP IDs, within
    three days of the transaction date) is accepted, so an unrelated number
    that happens to have 30 digits is not misread as a timestamp.
    """
    if mode == TimeFromReference.NIP:
        regex = NIP_SESSION_ID
    elif mode == TimeFromReference.PATTERN and pattern:
        try:
            regex = re.compile(pattern)
        except re.error:
            return None
    else:
        return None
    for text in texts:
        for m in regex.finditer(text or ""):
            parts = m.groupdict()
            try:
                found = time(int(parts["HH"]), int(parts["MM"]), int(parts.get("SS") or 0))
                if parts.get("YY") and parts.get("MO") and parts.get("DD"):
                    embedded = date(2000 + int(parts["YY"]), int(parts["MO"]), int(parts["DD"]))
                    if txn_date and abs((embedded - txn_date).days) > 3:
                        continue
            except (KeyError, TypeError, ValueError):
                continue
            return found
    return None


def parse_date(value, formats=None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # ISO dates / timestamps (as written by the XLSX reader for native date cells) are unambiguous.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:.]+)?", text):
        try:
            return datetime.fromisoformat(text).date()
        except ValueError as exc:
            raise RowError(f"Unrecognised date {text!r}") from exc
    for fmt in formats or DEFAULT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # A date followed by a time ("17/10/2026 14:32:05"): parse the date part.
    head, _, tail = text.partition(" ")
    if tail and _TIME.search(tail):
        return parse_date(head, formats)
    # Excel serial date numbers that reached us as text
    if re.fullmatch(r"\d{5}(\.0+)?", text):
        serial = int(float(text))
        if 20000 < serial < 80000:
            return _EXCEL_EPOCH + timedelta(days=serial)
    raise RowError(f"Unrecognised date {text!r}")


def normalize_reference(value) -> str:
    """Upper-case and remove whitespace only; punctuation and leading zeros are meaningful."""
    return _WS.sub("", str(value or "")).upper()


def normalize_narration(value) -> str:
    return _WS.sub(" ", _NON_ALNUM.sub(" ", str(value or "").upper())).strip()


def _text(raw: dict, mapping: dict, field: str) -> str:
    column = mapping.get(field)
    if not column:
        return ""
    value = raw.get(column)
    return "" if value is None else str(value).strip()


def normalize_row(raw: dict, profile, places: int = 2):
    """
    Convert a raw source row into standard fields.

    Returns ``(normalized, errors)``. ``normalized`` uses JSON-friendly values
    (ISO dates, Decimal strings) so it can be stored on the import row.
    """
    mapping = profile.column_mapping or {}
    formats = profile.date_formats or None
    errors: list[str] = []
    out: dict = {}
    source_values: dict = {}

    def get_date(field, required=False):
        text = _text(raw, mapping, field)
        if text:
            source_values[field] = text
        if not text:
            if required:
                errors.append(f"{field.replace('_', ' ').capitalize()} is required.")
            return None
        try:
            return parse_date(text, formats)
        except RowError as exc:
            errors.append(f"{field}: {exc}")
            return None

    def get_amount(field):
        text = _text(raw, mapping, field)
        if not text:
            return None, None
        source_values[field] = text
        try:
            return parse_amount(text, places)
        except AmountParseError as exc:
            errors.append(f"{field}: {exc}")
            return None, None

    txn_date = get_date("transaction_date", required=True)
    out["transaction_date"] = txn_date
    out["value_date"] = get_date("value_date")
    out["posting_date"] = get_date("posting_date")
    out["settlement_date"] = get_date("settlement_date")

    debit = credit = ZERO
    mode = profile.amount_mode
    if mode == AmountMode.SEPARATE:
        d, d_hint = get_amount("debit_amount")
        c, c_hint = get_amount("credit_amount")
        d = d or ZERO
        c = c or ZERO
        if d < 0 or c < 0:
            errors.append("Negative values are not allowed in separate debit/credit columns.")
        elif d > 0 and c > 0:
            errors.append("Row has both a debit and a credit amount.")
        debit, credit = d, c
    elif mode == AmountMode.SIGNED:
        amount, hint = get_amount("amount")
        if amount is not None:
            if hint:
                side = Side.DEBIT if hint == "DR" else Side.CREDIT
                magnitude = abs(amount)
            else:
                positive = profile.signed_positive_is
                negative = Side.DEBIT if positive == Side.CREDIT else Side.CREDIT
                side = positive if amount > 0 else negative
                magnitude = abs(amount)
            if side == Side.DEBIT:
                debit = magnitude
            else:
                credit = magnitude
    elif mode == AmountMode.INDICATOR:
        amount, hint = get_amount("amount")
        indicator = _text(raw, mapping, "dr_cr_indicator").upper()
        debit_values = {v.upper() for v in (profile.indicator_debit_values or ["DR", "D", "DEBIT"])}
        credit_values = {v.upper() for v in (profile.indicator_credit_values or ["CR", "C", "CREDIT"])}
        if amount is not None:
            if amount < 0:
                errors.append("Amount must not be negative when a DR/CR indicator is used.")
            elif indicator in debit_values:
                debit = amount
            elif indicator in credit_values:
                credit = amount
            else:
                errors.append(f"Unrecognised DR/CR indicator {indicator!r}.")
    if not errors and debit == ZERO and credit == ZERO:
        errors.append("Row has no transaction amount.")

    out["debit_amount"] = debit
    out["credit_amount"] = credit
    out["signed_amount"] = credit - debit

    for field in ("gross_amount", "fee_amount", "tax_amount", "net_amount"):
        value, _ = get_amount(field)
        out[field] = abs(value) if value is not None else None

    out["account_reference"] = _text(raw, mapping, "account_reference") or profile.default_account_reference
    if not out["account_reference"]:
        errors.append("Account reference is required (map a column or set a profile default).")
    out["transaction_id"] = _text(raw, mapping, "transaction_id")
    out["external_reference"] = _text(raw, mapping, "external_reference")
    out["norm_transaction_id"] = normalize_reference(out["transaction_id"])
    out["norm_external_reference"] = normalize_reference(out["external_reference"])
    out["narration"] = _text(raw, mapping, "narration")
    out["norm_narration"] = normalize_narration(out["narration"])
    out["counterparty_reference"] = _text(raw, mapping, "counterparty_reference")
    out["settlement_batch_ref"] = normalize_reference(_text(raw, mapping, "settlement_batch_ref"))
    currency = (_text(raw, mapping, "currency") or profile.default_currency).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        errors.append(f"Invalid currency code {currency!r}.")
    out["currency"] = currency
    channel = _text(raw, mapping, "channel").upper()
    out["channel"] = channel or profile.channel or ""
    out["is_reversal"] = _text(raw, mapping, "is_reversal").upper() in _TRUE

    # Time of day: a mapped time column, else a time part in the date cell, else the reference.
    txn_time = None
    time_text = _text(raw, mapping, "transaction_time")
    date_text = _text(raw, mapping, "transaction_date")
    if time_text:
        source_values["transaction_time"] = time_text
        try:
            txn_time = parse_time(time_text)
        except RowError as exc:
            errors.append(f"transaction_time: {exc}")
    elif _TIME.search(date_text.partition("T")[2] or date_text.partition(" ")[2]):
        try:
            txn_time = parse_time(date_text)
        except RowError:
            txn_time = None
    if txn_time is None and not time_text:
        txn_time = time_from_reference(
            [out["transaction_id"], out["external_reference"], out["narration"]],
            getattr(profile, "time_from_reference", TimeFromReference.NONE),
            getattr(profile, "reference_time_pattern", ""),
            txn_date,
        )
        if txn_time is not None:
            source_values["transaction_time"] = "derived from reference"
    out["transaction_time"] = txn_time
    out["source_values"] = source_values
    return out, errors


def content_key(institution_id: int, source_system: str, n: dict) -> str:
    parts = [
        str(institution_id),
        source_system.upper(),
        n["account_reference"].upper(),
        str(n["transaction_date"]),
        str(n["debit_amount"]),
        str(n["credit_amount"]),
        n["currency"],
    ]
    if n["norm_transaction_id"]:
        parts.append("ID:" + n["norm_transaction_id"])
    else:
        parts += ["EXT:" + n["norm_external_reference"], "NAR:" + n["norm_narration"]]
    return "|".join(parts)


def fingerprint(key: str, occurrence: int) -> str:
    """
    Duplicate fingerprint. ``occurrence`` distinguishes genuinely repeated,
    identical lines within one source file (e.g. two identical fees on the
    same day) so that re-importing the same data is still idempotent.
    """
    return hashlib.sha256(f"{key}#{occurrence}".encode()).hexdigest()


def to_jsonable(normalized: dict) -> dict:
    out = {}
    for key, value in normalized.items():
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, date | time):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def from_jsonable(data: dict) -> dict:
    out = dict(data)
    for key in ("transaction_date", "value_date", "posting_date", "settlement_date"):
        if out.get(key):
            out[key] = date.fromisoformat(out[key])
    if out.get("transaction_time"):
        out["transaction_time"] = time.fromisoformat(out["transaction_time"])
    for key in (
        "debit_amount",
        "credit_amount",
        "signed_amount",
        "gross_amount",
        "fee_amount",
        "tax_amount",
        "net_amount",
    ):
        if out.get(key) is not None:
            out[key] = Decimal(out[key])
    return out
