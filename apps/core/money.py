"""
Decimal-only money helpers.

Financial amounts in LedgerGuard never pass through binary floating point.
Parsing accepts the formats commonly found in Nigerian bank statements and core
banking exports ("1,250,000.00", "(5,000.00)", "₦12,500", "5,000.00 DR").
"""

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")

MONEY_MAX_DIGITS = 20
MONEY_DECIMAL_PLACES = 2

ROUNDING_MODES = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
}

_CURRENCY_TOKENS = re.compile(r"(NGN|USD|GBP|EUR|₦|\$|£|€)", re.IGNORECASE)
_DRCR_SUFFIX = re.compile(r"\s*(DR|CR|D|C)\.?$", re.IGNORECASE)


class AmountParseError(ValueError):
    pass


def quantize(value, places: int = MONEY_DECIMAL_PLACES, rounding: str = "HALF_UP") -> Decimal:
    if not isinstance(value, Decimal):
        if isinstance(value, float):
            raise TypeError("Floats are not accepted for financial amounts; use Decimal or str.")
        value = Decimal(value)
    exponent = Decimal(1).scaleb(-places)
    return value.quantize(exponent, rounding=ROUNDING_MODES[rounding])


def parse_amount(raw, places: int = MONEY_DECIMAL_PLACES) -> tuple[Decimal | None, str | None]:
    """
    Parse a source amount into a Decimal.

    Returns ``(amount, drcr_hint)`` where ``drcr_hint`` is "DR"/"CR" when the
    value carried an explicit suffix, otherwise ``None``. Empty input returns
    ``(None, None)``. Raises :class:`AmountParseError` on malformed values.
    """
    if raw is None:
        return None, None
    if isinstance(raw, float):
        # openpyxl returns floats for numeric cells; repr() gives the shortest
        # round-tripping string, which is then parsed exactly as Decimal.
        raw = repr(raw)
    if isinstance(raw, int | Decimal):
        return quantize(Decimal(raw), places), None
    text = str(raw).strip()
    if not text or text in {"-", "--"}:
        return None, None

    hint = None
    m = _DRCR_SUFFIX.search(text)
    if m and re.search(r"\d", text[: m.start()]):
        token = m.group(1).upper()
        hint = "DR" if token.startswith("D") else "CR"
        text = text[: m.start()]

    negative = False
    text = _CURRENCY_TOKENS.sub("", text).strip()
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()
    text = text.replace(",", "").replace(" ", "")
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", text):
        raise AmountParseError(f"Invalid amount: {raw!r}")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex
        raise AmountParseError(f"Invalid amount: {raw!r}") from exc
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > places:
        # Do not silently round source data with more precision than configured.
        if value != quantize(value, places):
            raise AmountParseError(f"Amount {raw!r} has more than {places} decimal places")
    value = quantize(value, places)
    return (-value if negative else value), hint


def format_money(value: Decimal | None, places: int = MONEY_DECIMAL_PLACES) -> str:
    if value is None:
        return ""
    return f"{quantize(value, places):,.{places}f}"
