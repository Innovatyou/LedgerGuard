"""
Regulatory workpapers: compute template lines from posted ledger balances,
evaluate formulas and validation rules safely, and record preparer/reviewer
sign-off. LedgerGuard never submits returns to a regulator.
"""

import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounting.models import LedgerEntry
from apps.accounts.services import require_perm
from apps.approvals.services import assert_independent
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError

from .models import RegulatoryReturn, RegulatoryTemplate, TemplateLine, ValidationRule

ZERO = Decimal("0.00")
_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z][A-Za-z0-9_]*)|(.))")


class FormulaError(ValueError):
    pass


def evaluate(expression: str, values: dict) -> Decimal:
    """
    Evaluate ``+ - * /`` and parentheses over Decimal numbers and line codes.
    A tiny recursive-descent parser – no use of Python ``eval``.
    """
    tokens = []
    for number, name, other in _TOKEN.findall(expression or ""):
        if number:
            tokens.append(("num", Decimal(number)))
        elif name:
            if name not in values:
                raise FormulaError(f"Unknown line code {name}")
            tokens.append(("num", Decimal(values[name] or 0)))
        elif other.strip():
            if other not in "+-*/()":
                raise FormulaError(f"Unexpected character {other!r}")
            tokens.append(("op", other))
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else (None, None)

    def take():
        nonlocal pos
        token = peek()
        pos += 1
        return token

    def factor():
        kind, value = take()
        if kind == "num":
            return value
        if (kind, value) == ("op", "-"):
            return -factor()
        if (kind, value) == ("op", "+"):
            return factor()
        if (kind, value) == ("op", "("):
            result = expr()
            if take() != ("op", ")"):
                raise FormulaError("Missing closing parenthesis")
            return result
        raise FormulaError("Incomplete expression")

    def term():
        result = factor()
        while peek() in (("op", "*"), ("op", "/")):
            _, op = take()
            rhs = factor()
            if op == "*":
                result *= rhs
            else:
                if rhs == 0:
                    raise FormulaError("Division by zero")
                result /= rhs
        return result

    def expr():
        result = term()
        while peek() in (("op", "+"), ("op", "-")):
            _, op = take()
            result = result + term() if op == "+" else result - term()
        return result

    if not tokens:
        return ZERO
    value = expr()
    if pos != len(tokens):
        raise FormulaError("Unexpected trailing input")
    return value


def _line_value(line: TemplateLine, institution, period_start, period_end) -> Decimal:
    q = Q()
    for prefix in line.regulatory_codes or []:
        q |= Q(account__regulatory_code__startswith=prefix)
    for sub in line.subcategories or []:
        q |= Q(account__subcategory=sub)
    for prefix in line.account_codes or []:
        q |= Q(account__code__startswith=prefix)
    if not q and not line.categories:
        return ZERO
    entries = LedgerEntry.objects.filter(q, institution=institution, accounting_date__lte=period_end)
    if line.categories:
        entries = entries.filter(account__category__in=line.categories)
    if line.basis == TemplateLine.Basis.MOVEMENT:
        entries = entries.filter(accounting_date__gte=period_start)
    agg = entries.aggregate(dr=Sum("debit"), cr=Sum("credit"))
    net = (agg["dr"] or ZERO) - (agg["cr"] or ZERO)
    return net if line.sign == TemplateLine.Sign.DEBIT_POSITIVE else -net


def compute(template: RegulatoryTemplate, institution, period_start, period_end, manual: dict) -> dict:
    values: dict[str, Decimal] = {}
    lines = list(template.lines.all())
    for line in lines:
        if line.kind == TemplateLine.Kind.ACCOUNTS:
            values[line.code] = _line_value(line, institution, period_start, period_end)
        elif line.kind == TemplateLine.Kind.MANUAL:
            try:
                values[line.code] = Decimal(str(manual.get(line.code) or "0"))
            except InvalidOperation as exc:
                raise BusinessRuleError(f"Manual value for {line.code} is not a number.") from exc
    pending = [ln for ln in lines if ln.kind == TemplateLine.Kind.FORMULA]
    for _ in range(len(pending) + 1):  # resolve formulas that depend on other formulas
        unresolved = []
        for line in pending:
            try:
                values[line.code] = evaluate(line.formula, values).quantize(Decimal("0.01"))
            except FormulaError as exc:
                if str(exc).startswith("Unknown line code"):
                    unresolved.append(line)
                else:
                    raise BusinessRuleError(f"Line {line.code}: {exc}") from exc
        if not unresolved:
            break
        if len(unresolved) == len(pending):
            raise BusinessRuleError(
                "Formulas refer to unknown or circular lines: " + ", ".join(ln.code for ln in unresolved)
            )
        pending = unresolved
    return values


def run_validations(template: RegulatoryTemplate, values: dict) -> list[dict]:
    results = []
    for rule in template.rules.all():
        match = re.fullmatch(r"(.+?)(>=|<=|=)(.+)", rule.expression.strip())
        if not match:
            results.append({"rule": rule.name, "passed": False, "detail": "Invalid rule expression"})
            continue
        try:
            lhs, rhs = evaluate(match.group(1), values), evaluate(match.group(3), values)
        except FormulaError as exc:
            results.append({"rule": rule.name, "passed": False, "detail": str(exc)})
            continue
        op, diff = match.group(2), lhs - rhs
        passed = {"=": abs(diff) <= rule.tolerance, ">=": diff >= -rule.tolerance, "<=": diff <= rule.tolerance}[op]
        results.append(
            {
                "rule": rule.name,
                "expression": rule.expression,
                "passed": passed,
                "lhs": str(lhs.quantize(Decimal("0.01"))),
                "rhs": str(rhs.quantize(Decimal("0.01"))),
                "difference": str(diff.quantize(Decimal("0.01"))),
            }
        )
    return results


def prepare(institution, user, template, period_start, period_end, manual=None, notes="") -> RegulatoryReturn:
    require_perm(user, institution, "regulatory.manage")
    if template.institution_id != institution.pk:
        raise BusinessRuleError("Template belongs to a different institution.")
    if period_end < period_start:
        raise BusinessRuleError("Period end precedes period start.")
    with transaction.atomic():
        ret = (
            RegulatoryReturn.objects.select_for_update()
            .filter(institution=institution, template=template, period_end=period_end)
            .first()
        )
        if ret and ret.status == RegulatoryReturn.Status.REVIEWED:
            raise BusinessRuleError("This workpaper has been signed off and cannot be regenerated.")
        manual = {**(ret.manual_values if ret else {}), **(manual or {})}
        values = compute(template, institution, period_start, period_end, manual)
        results = run_validations(template, values)
        if ret is None:
            ret = RegulatoryReturn(institution=institution, template=template, period_end=period_end, prepared_by=user)
        ret.period_start = period_start
        ret.values = {k: str(v) for k, v in values.items()}
        ret.manual_values = {k: str(v) for k, v in manual.items()}
        ret.validation_results = results
        ret.generated_at = timezone.now()
        ret.prepared_by = user
        ret.status = RegulatoryReturn.Status.PREPARED
        ret.notes = notes or ret.notes
        ret.save()
    log_activity(
        "regulatory.prepared",
        user=user,
        obj=ret,
        details={"passed": ret.passed, "failed_rules": [r["rule"] for r in results if not r["passed"]]},
    )
    return ret


def review(ret: RegulatoryReturn, user, comment="") -> RegulatoryReturn:
    require_perm(user, ret.institution, "regulatory.review")
    if ret.status != RegulatoryReturn.Status.PREPARED:
        raise BusinessRuleError("Only prepared workpapers can be signed off.")
    assert_independent(ret.institution, ret.prepared_by, user, "this workpaper")
    if not ret.passed and not comment.strip():
        raise BusinessRuleError("Validation rules failed; a reviewer comment explaining the exception is required.")
    ret.status = RegulatoryReturn.Status.REVIEWED
    ret.reviewed_by = user
    ret.reviewed_at = timezone.now()
    if comment:
        ret.notes = (ret.notes + "\n" if ret.notes else "") + f"Reviewer: {comment}"
    ret.save()
    log_activity("regulatory.reviewed", user=user, obj=ret, details={"comment": comment})
    return ret


# Illustrative starter template keyed on account subcategories (works with the bundled chart templates).
STARTER = {
    "code": "SFP-MONTHLY",
    "name": "Monthly statement of financial position summary (illustrative)",
    "regulator": "CBN",
    "lines": [
        ("H1", "ASSETS", "HEADING", {}),
        ("A100", "Cash and cash equivalents", "ACCOUNTS", {"subcategories": ["CASH"]}),
        ("A110", "Balances with the Central Bank", "ACCOUNTS", {"subcategories": ["BALANCES_WITH_CBN"]}),
        ("A200", "Balances with other financial institutions", "ACCOUNTS", {"subcategories": ["DUE_FROM_BANKS"]}),
        ("A250", "Investment securities", "ACCOUNTS", {"subcategories": ["INVESTMENTS"]}),
        ("A300", "Loans and advances (gross)", "ACCOUNTS", {"subcategories": ["LOANS_ADVANCES"]}),
        ("A310", "Less: impairment allowance", "ACCOUNTS", {"subcategories": ["LOAN_IMPAIRMENT"]}),
        (
            "A400",
            "Property and equipment (net)",
            "ACCOUNTS",
            {"subcategories": ["FIXED_ASSETS", "ACCUMULATED_DEPRECIATION"]},
        ),
        (
            "A500",
            "Other assets",
            "ACCOUNTS",
            {"subcategories": ["OTHER_ASSETS", "SETTLEMENT", "SUSPENSE", "OTHER"], "categories": ["ASSET"]},
        ),
        (
            "A900",
            "TOTAL ASSETS",
            "FORMULA",
            {"formula": "A100 + A110 + A200 + A250 + A300 + A310 + A400 + A500", "is_total": True},
        ),
        ("H2", "LIABILITIES AND EQUITY", "HEADING", {}),
        ("L100", "Customer deposits", "ACCOUNTS", {"subcategories": ["CUSTOMER_DEPOSITS"], "sign": "CREDIT_POSITIVE"}),
        ("L200", "Borrowings", "ACCOUNTS", {"subcategories": ["BORROWINGS"], "sign": "CREDIT_POSITIVE"}),
        (
            "L300",
            "Other liabilities",
            "ACCOUNTS",
            {
                "subcategories": ["OTHER_LIABILITIES", "PROVISIONS", "TAX_PAYABLE", "SUSPENSE", "OTHER"],
                "categories": ["LIABILITY"],
                "sign": "CREDIT_POSITIVE",
            },
        ),
        ("L900", "TOTAL LIABILITIES", "FORMULA", {"formula": "L100 + L200 + L300", "is_total": True}),
        (
            "E100",
            "Share capital and reserves",
            "ACCOUNTS",
            {"subcategories": ["SHARE_CAPITAL", "RESERVES", "RETAINED_EARNINGS"], "sign": "CREDIT_POSITIVE"},
        ),
        (
            "E100X",
            "Other equity",
            "ACCOUNTS",
            {"subcategories": ["OTHER"], "categories": ["EQUITY"], "sign": "CREDIT_POSITIVE"},
        ),
        (
            "E200",
            "Profit not yet transferred to retained earnings",
            "ACCOUNTS",
            {"categories": ["INCOME", "EXPENSE"], "sign": "CREDIT_POSITIVE"},
        ),
        ("E900", "TOTAL EQUITY", "FORMULA", {"formula": "E100 + E100X + E200", "is_total": True}),
        ("T900", "TOTAL LIABILITIES AND EQUITY", "FORMULA", {"formula": "L900 + E900", "is_total": True}),
        ("R10", "Loans to deposits ratio (%)", "FORMULA", {"formula": "(A300 + A310) / L100 * 100"}),
    ],
    "rules": [("Balance sheet balances", "A900 = T900", "0.00")],
}


def install_starter_template(institution, user) -> RegulatoryTemplate:
    """Install the illustrative template (every ledger account maps to exactly one line)."""
    require_perm(user, institution, "regulatory.manage")
    if RegulatoryTemplate.objects.filter(institution=institution, code=STARTER["code"]).exists():
        raise BusinessRuleError("The starter template is already installed.")
    with transaction.atomic():
        template = RegulatoryTemplate.objects.create(
            institution=institution, code=STARTER["code"], name=STARTER["name"], regulator=STARTER["regulator"]
        )
        for order, (code, label, kind, opts) in enumerate(STARTER["lines"], start=1):
            TemplateLine.objects.create(template=template, code=code, label=label, kind=kind, order=order * 10, **opts)
        for name, expression, tolerance in STARTER["rules"]:
            ValidationRule.objects.create(
                template=template, name=name, expression=expression, tolerance=Decimal(tolerance)
            )
    log_activity("regulatory.template_installed", user=user, obj=template)
    return template
