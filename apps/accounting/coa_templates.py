"""
Starter chart-of-accounts templates by institution type.

IMPORTANT: these templates are starting points only. Each institution must
validate the accounts, mappings and regulatory codes against its own
accounting policies, IFRS reporting requirements and applicable CBN / NDIC
reporting templates before use.
"""

from django.db import transaction

from apps.accounts.services import require_perm
from apps.core.activity import log_activity

from .models import Account, AccountCategory, AccountType, CashFlowClass

TEMPLATE_WARNING = (
    "Template accounts must be reviewed and validated against the institution's accounting policies "
    "and applicable regulatory requirements before use."
)

A, L, E, I, X = (  # noqa: E741 - short aliases keep the template table readable
    AccountCategory.ASSET,
    AccountCategory.LIABILITY,
    AccountCategory.EQUITY,
    AccountCategory.INCOME,
    AccountCategory.EXPENSE,
)
H = AccountType.HEADER

# (code, name, category, subcategory, parent_code, options)
_COMMON = [
    ("1000", "Assets", A, "OTHER", None, {"type": H}),
    ("1100", "Cash and cash equivalents", A, "CASH", "1000", {"type": H}),
    ("1101", "Cash in vault", A, "CASH", "1100", {"cf": "CASH", "recon": True}),
    ("1102", "Cash in ATMs", A, "CASH", "1100", {"cf": "CASH", "recon": True}),
    ("1200", "Balances with other financial institutions", A, "DUE_FROM_BANKS", "1000", {"type": H}),
    ("1201", "Current account with correspondent bank", A, "DUE_FROM_BANKS", "1200", {"cf": "CASH", "recon": True}),
    ("1300", "Loans and advances", A, "LOANS_ADVANCES", "1000", {"type": H}),
    (
        "1301",
        "Loans and advances to customers",
        A,
        "LOANS_ADVANCES",
        "1300",
        {"control": True, "recon": True, "cf": "OPERATING"},
    ),
    ("1309", "Impairment allowance on loans", A, "LOAN_IMPAIRMENT", "1300", {"normal": "CREDIT", "cf": "OPERATING"}),
    ("1400", "Property and equipment", A, "FIXED_ASSETS", "1000", {"type": H}),
    ("1401", "Property and equipment – cost", A, "FIXED_ASSETS", "1400", {"cf": "INVESTING"}),
    (
        "1409",
        "Accumulated depreciation",
        A,
        "ACCUMULATED_DEPRECIATION",
        "1400",
        {"normal": "CREDIT", "cf": "INVESTING"},
    ),
    ("1500", "Settlement and suspense", A, "OTHER_ASSETS", "1000", {"type": H}),
    ("1501", "NIP settlement account", A, "SETTLEMENT", "1500", {"recon": True, "control": True}),
    ("1502", "POS settlement account", A, "SETTLEMENT", "1500", {"recon": True, "control": True}),
    ("1503", "ATM settlement account", A, "SETTLEMENT", "1500", {"recon": True, "control": True}),
    ("1590", "Unidentified receipts suspense", A, "SUSPENSE", "1500", {"suspense": True, "recon": True}),
    ("1600", "Other assets", A, "OTHER_ASSETS", "1000", {"type": H}),
    ("1601", "Prepayments", A, "OTHER_ASSETS", "1600", {"cf": "OPERATING"}),
    ("1602", "Other receivables", A, "OTHER_ASSETS", "1600", {"cf": "OPERATING"}),
    ("2000", "Liabilities", L, "OTHER", None, {"type": H}),
    ("2100", "Customer deposits", L, "CUSTOMER_DEPOSITS", "2000", {"type": H}),
    ("2101", "Current accounts", L, "CUSTOMER_DEPOSITS", "2100", {"control": True, "recon": True, "cf": "OPERATING"}),
    ("2102", "Savings accounts", L, "CUSTOMER_DEPOSITS", "2100", {"control": True, "recon": True, "cf": "OPERATING"}),
    ("2103", "Term deposits", L, "CUSTOMER_DEPOSITS", "2100", {"control": True, "recon": True, "cf": "OPERATING"}),
    ("2200", "Borrowings", L, "BORROWINGS", "2000", {"type": H}),
    ("2201", "Borrowings from financial institutions", L, "BORROWINGS", "2200", {"cf": "FINANCING"}),
    ("2300", "Other liabilities", L, "OTHER_LIABILITIES", "2000", {"type": H}),
    ("2301", "Accrued expenses", L, "OTHER_LIABILITIES", "2300", {"cf": "OPERATING"}),
    ("2302", "Withholding tax payable", L, "TAX_PAYABLE", "2300", {"cf": "OPERATING"}),
    ("2303", "VAT payable", L, "TAX_PAYABLE", "2300", {"cf": "OPERATING"}),
    ("2390", "Unidentified credits suspense", L, "SUSPENSE", "2300", {"suspense": True, "recon": True}),
    ("3000", "Equity", E, "OTHER", None, {"type": H}),
    ("3101", "Share capital", E, "SHARE_CAPITAL", "3000", {"cf": "FINANCING"}),
    ("3201", "Statutory reserve", E, "RESERVES", "3000", {}),
    ("3301", "Retained earnings", E, "RETAINED_EARNINGS", "3000", {}),
    ("4000", "Income", I, "OTHER", None, {"type": H}),
    ("4101", "Interest income on loans", I, "INTEREST_INCOME", "4000", {}),
    ("4102", "Interest income on placements", I, "INTEREST_INCOME", "4000", {}),
    ("4201", "Fees and commission income", I, "FEE_COMMISSION_INCOME", "4000", {}),
    ("4202", "Electronic channel fees", I, "FEE_COMMISSION_INCOME", "4000", {}),
    ("4901", "Other income", I, "OTHER_INCOME", "4000", {}),
    ("5000", "Expenses", X, "OTHER", None, {"type": H}),
    ("5101", "Interest expense on deposits", X, "INTEREST_EXPENSE", "5000", {}),
    ("5102", "Interest expense on borrowings", X, "INTEREST_EXPENSE", "5000", {}),
    ("5201", "Fees and commission expense", X, "FEE_COMMISSION_EXPENSE", "5000", {}),
    ("5301", "Impairment charge on loans", X, "IMPAIRMENT_CHARGE", "5000", {}),
    ("5401", "Salaries and wages", X, "PERSONNEL_EXPENSE", "5000", {}),
    ("5501", "Depreciation expense", X, "DEPRECIATION_EXPENSE", "5000", {}),
    ("5601", "Rent and premises", X, "OPERATING_EXPENSE", "5000", {}),
    ("5602", "IT and communication", X, "OPERATING_EXPENSE", "5000", {}),
    ("5603", "Insurance premium (incl. deposit insurance)", X, "OPERATING_EXPENSE", "5000", {}),
    ("5901", "Income tax expense", X, "INCOME_TAX", "5000", {}),
]

_COMMERCIAL_EXTRA = [
    ("1150", "Balances with the Central Bank", A, "BALANCES_WITH_CBN", "1000", {"type": H}),
    ("1151", "Current account with CBN", A, "BALANCES_WITH_CBN", "1150", {"cf": "CASH", "recon": True}),
    ("1152", "Cash reserve requirement", A, "BALANCES_WITH_CBN", "1150", {"cf": "OPERATING"}),
    ("1250", "Investment securities", A, "INVESTMENTS", "1000", {"type": H}),
    ("1251", "Treasury bills", A, "INVESTMENTS", "1250", {"cf": "INVESTING"}),
    ("1504", "Card scheme settlement account", A, "SETTLEMENT", "1500", {"recon": True, "control": True}),
    ("1505", "Interbank clearing account", A, "SETTLEMENT", "1500", {"recon": True, "control": True}),
]

_MFB_EXTRA = [
    (
        "1303",
        "Group / cooperative loans",
        A,
        "LOANS_ADVANCES",
        "1300",
        {"control": True, "recon": True, "cf": "OPERATING"},
    ),
    ("2104", "Mandatory savings (loan collateral)", L, "CUSTOMER_DEPOSITS", "2100", {"control": True, "recon": True}),
]

_OFI_EXTRA = [
    ("1302", "Lease receivables", A, "LOANS_ADVANCES", "1300", {"control": True, "recon": True, "cf": "OPERATING"}),
    ("2202", "Commercial paper issued", L, "BORROWINGS", "2200", {"cf": "FINANCING"}),
]

TEMPLATES = {
    "COMMERCIAL_BANK": ("Commercial bank starter chart", _COMMON + _COMMERCIAL_EXTRA),
    "MFB": ("Microfinance bank starter chart", _COMMON + _MFB_EXTRA),
    "OFI": ("Other financial institution starter chart", _COMMON + _OFI_EXTRA),
    "FINANCE_COMPANY": ("Finance company starter chart", _COMMON + _OFI_EXTRA),
}


def apply_template(institution, user, template_key: str) -> int:
    """Create template accounts that do not already exist. Returns number created."""
    require_perm(user, institution, "coa.manage")
    if template_key not in TEMPLATES:
        raise ValueError(f"Unknown template {template_key}")
    _, rows = TEMPLATES[template_key]
    created = 0
    with transaction.atomic():
        by_code = {a.code: a for a in Account.objects.filter(institution=institution)}
        for code, name, category, sub, parent_code, opts in sorted(rows, key=lambda r: (r[4] is not None, r[0])):
            if code in by_code:
                continue
            account = Account(
                institution=institution,
                code=code,
                name=name,
                category=category,
                subcategory=sub,
                parent=by_code.get(parent_code),
                account_type=opts.get("type", AccountType.POSTING),
                normal_balance=opts.get("normal", ""),
                reconciliation_required=opts.get("recon", False),
                is_control_account=opts.get("control", False),
                is_suspense_account=opts.get("suspense", False),
                cash_flow_class=CashFlowClass(opts["cf"]) if "cf" in opts else "",
                created_by=user,
            )
            account.save()
            by_code[code] = account
            created += 1
    log_activity(
        "coa.template_applied",
        user=user,
        institution=institution,
        details={"template": template_key, "created": created},
    )
    return created
