"""Builders that turn report objects into exportable tables."""

from decimal import Decimal

from apps.accounting.reports import FinancialPosition, GeneralLedger, IncomeStatement, TrialBalance

from .exports import Column, Table

ZERO = Decimal("0.00")

ZERO_NOTE = "All figures are derived from posted LedgerGuard ledger entries in the institution's base currency."


def trial_balance_table(tb: TrialBalance, institution, user) -> Table:
    table = Table(
        title="Trial Balance",
        subtitle=f"As at {tb.as_of:%d %B %Y}",
        columns=[
            Column("Code", width=10),
            Column("Account", width=40),
            Column("Category", width=14),
            Column("Total debits", "money"),
            Column("Total credits", "money"),
            Column("Debit balance", "money"),
            Column("Credit balance", "money"),
        ],
        institution=institution.name,
        generated_by=str(user),
        notes=[ZERO_NOTE],
    )
    for row in tb.rows:
        table.rows.append(
            [
                row.account.code,
                row.account.name,
                row.account.get_category_display(),
                row.debit_total,
                row.credit_total,
                row.balance_debit,
                row.balance_credit,
            ]
        )
    table.totals = ["", "Totals", "", None, None, tb.total_debit, tb.total_credit]
    if not tb.is_balanced:
        table.notes.append("WARNING: the trial balance does not balance.")
    return table


def general_ledger_table(gl: GeneralLedger, institution, user) -> Table:
    period = f"{gl.date_from or 'inception'} to {gl.date_to or 'date'}"
    table = Table(
        title=f"General Ledger – {gl.account.code} {gl.account.name}",
        subtitle=f"Period {period}",
        columns=[
            Column("Date", "date", 12),
            Column("Journal", width=16),
            Column("Description", width=40),
            Column("Debit", "money"),
            Column("Credit", "money"),
            Column("Balance", "money"),
        ],
        institution=institution.name,
        generated_by=str(user),
        notes=[f"Balances shown in the account's normal direction ({gl.account.get_normal_balance_display()})."],
    )
    table.rows.append([gl.date_from, "", "Opening balance", None, None, gl.opening_balance])
    for line in gl.lines:
        e = line.entry
        table.rows.append(
            [
                e.accounting_date,
                e.journal.number,
                e.journal.description,
                e.debit or None,
                e.credit or None,
                line.running_balance,
            ]
        )
    table.totals = ["", "", "Closing balance", gl.total_debit, gl.total_credit, gl.closing_balance]
    return table


def income_statement_table(stmt: IncomeStatement, institution, user) -> Table:
    table = Table(
        title="Income Statement",
        subtitle=f"For the period {stmt.date_from:%d %b %Y} to {stmt.date_to:%d %b %Y}",
        columns=[Column("Line", width=40), Column("Account", width=40), Column("Amount", "money")],
        institution=institution.name,
        generated_by=str(user),
        notes=[ZERO_NOTE],
    )
    for section in (stmt.income, stmt.expenses):
        table.rows.append([section.title.upper(), "", None])
        for group, total, items in section.group_totals():
            for account, amount in items:
                table.rows.append([group, f"{account.code} {account.name}", amount])
            table.rows.append([f"Total {group}", "", total])
        table.rows.append([f"TOTAL {section.title.upper()}", "", section.total])
    table.totals = ["Net profit / (loss)", "", stmt.net_profit]
    return table


def financial_position_table(fp: FinancialPosition, institution, user) -> Table:
    table = Table(
        title="Statement of Financial Position",
        subtitle=f"As at {fp.as_of:%d %B %Y}",
        columns=[Column("Line", width=40), Column("Account", width=40), Column("Amount", "money")],
        institution=institution.name,
        generated_by=str(user),
        notes=[ZERO_NOTE],
    )
    for section in (fp.assets, fp.liabilities, fp.equity):
        table.rows.append([section.title.upper(), "", None])
        for group, total, items in section.group_totals():
            for account, amount in items:
                table.rows.append([group, f"{account.code} {account.name}", amount])
            table.rows.append([f"Total {group}", "", total])
        if section is fp.equity:
            table.rows.append(["Current earnings not yet closed to retained earnings", "", fp.unclosed_earnings])
            table.rows.append(["TOTAL EQUITY", "", fp.total_equity])
        else:
            table.rows.append([f"TOTAL {section.title.upper()}", "", section.total])
    table.totals = ["Total liabilities and equity", "", fp.total_liabilities_and_equity]
    if not fp.is_balanced:
        table.notes.append(f"WARNING: assets differ from liabilities and equity by {fp.difference}.")
    return table


def reconciliation_table(stmt, institution, user) -> Table:
    recon = stmt.recon
    table = Table(
        title=f"Reconciliation Statement {recon.reference}",
        subtitle=f"{recon.name} – {recon.period_start:%d %b %Y} to {recon.period_end:%d %b %Y} ({recon.currency})",
        columns=[
            Column("Item", width=48),
            Column("Date", "date", 12),
            Column("Reference", width=20),
            Column("Amount", "money"),
        ],
        institution=institution.name,
        generated_by=str(user),
    )
    L, R = stmt.left, stmt.right
    rows = table.rows
    rows += [
        [f"{L.label}: opening balance", recon.period_start, "", L.opening],
        [f"{L.label}: inflows (credits)", None, "", L.inflows],
        [f"{L.label}: outflows (debits)", None, "", L.outflows],
        [f"{L.label}: closing balance", recon.period_end, "", L.closing],
        [f"{R.label}: opening balance", recon.period_start, "", R.opening],
        [f"{R.label}: inflows", None, "", R.inflows],
        [f"{R.label}: outflows", None, "", R.outflows],
        [f"{R.label}: closing balance", recon.period_end, "", R.closing],
        [f"Matched groups: {stmt.matched_groups}", None, "", None],
    ]

    def section(title, items):
        rows.append([title, None, "", sum((f for _, f in items), ZERO)])
        for txn, flow in items:
            rows.append(
                [f"   {txn.narration[:60]}", txn.transaction_date, txn.transaction_id or txn.external_reference, flow]
            )

    rows.append([f"{L.label} closing balance", None, "", L.closing])
    section(f"Add: outstanding deposits (in {R.label}, not in {L.label})", stmt.outstanding_deposits)
    section(f"Less: outstanding payments (in {R.label}, not in {L.label})", stmt.outstanding_payments)
    section(f"Excluded {R.label} items (authorised reasons)", stmt.excluded_right)
    rows.append([f"Adjustments to {L.label}", None, "", stmt.left_adjustments])
    rows.append([f"Adjusted {L.label} balance", None, "", stmt.adjusted_left_balance])
    rows.append([f"{R.label} closing balance", None, "", R.closing])
    section(f"Add: unrecorded credits (in {L.label}, not in {R.label})", stmt.unrecorded_credits)
    section(f"Less: unrecorded charges (in {L.label}, not in {R.label})", stmt.unrecorded_charges)
    section(f"Excluded {L.label} items (authorised reasons)", stmt.excluded_left)
    rows.append([f"Adjustments to {R.label}", None, "", stmt.right_adjustments])
    rows.append([f"Adjusted {R.label} balance", None, "", stmt.adjusted_right_balance])
    section("Suspected duplicates (included above)", stmt.duplicates)
    table.totals = ["Final reconciliation difference", "", "", stmt.final_difference]
    table.notes.append(
        f"Prepared by {recon.prepared_by}; status {recon.get_status_display()}"
        + (f"; approved by {recon.approved_by} on {recon.approved_at:%d %b %Y}" if recon.approved_by else "")
    )
    for err in stmt.validation_errors(institution.reconciliation_tolerance):
        table.notes.append(f"Validation: {err}")
    return table


def aging_table(report, institution, user) -> Table:
    table = Table(
        title="Suspense Aging",
        subtitle=f"As at {report.as_of:%d %B %Y}",
        columns=[
            Column("Reference", width=14),
            Column("Account", width=30),
            Column("Date", "date", 12),
            Column("Transaction ref", width=18),
            Column("Amount", "money"),
            Column("Side", width=8),
            Column("Age (days)", "int", 10),
            Column("Bucket", width=14),
            Column("Assigned to", width=18),
            Column("Status", width=16),
        ],
        institution=institution.name,
        generated_by=str(user),
    )
    for item, age, label in report.items:
        table.rows.append(
            [
                item.reference,
                str(item.account),
                item.original_date,
                item.transaction_reference,
                item.original_amount,
                item.get_side_display(),
                age,
                label,
                str(item.assigned_to or ""),
                item.get_status_display(),
            ]
        )
    for label, count, total in zip(report.labels, report.bucket_counts, report.bucket_totals, strict=True):
        table.notes.append(f"{label}: {count} item(s), {total:,.2f}")
    table.totals = ["Total", "", None, "", report.total, "", None, "", "", ""]
    return table


def session_table(report, recon, institution, user) -> Table:
    rule = report.rule
    calendar = ", ".join(rule.session_cutoffs) if rule else "none (grouped by day)"
    table = Table(
        title=f"Settlement Sessions {recon.reference}",
        subtitle=f"{recon.name} – {recon.period_start:%d %b %Y} to {recon.period_end:%d %b %Y} ({recon.currency})",
        columns=[
            Column("Session date", "date", 12),
            Column("Session", width=10),
            Column("Direction", width=10),
            Column("Items", "int", 8),
            Column("Items total", "money"),
            Column("Matched", "int", 8),
            Column("Proposed", "int", 8),
            Column("Bulk lines", "int", 8),
            Column("Bulk total", "money"),
            Column("Difference", "money"),
            Column("Status", width=14),
        ],
        institution=institution.name,
        generated_by=str(user),
    )
    for r in report.rows:
        table.rows.append(
            [
                r.session_date,
                str(r.session) if r.session else "Day",
                r.direction,
                r.items,
                r.items_total,
                r.accepted,
                r.proposed,
                r.bulk_lines,
                r.bulk_total,
                r.difference,
                r.status,
            ]
        )
    for txn, flow in report.unmatched_bulk:
        ref = txn.transaction_id or txn.external_reference or txn.narration[:40]
        table.rows.append(
            [txn.transaction_date, "—", f"Unmatched line {ref}", 0, ZERO, 0, 0, 1, flow, -flow, "Unmatched"]
        )
    table.notes.append(f"Session cut-off times: {calendar}. Amounts are inflow-positive.")
    if report.untimed_items:
        table.notes.append(
            f"{report.untimed_items} item(s) totalling {report.untimed_total:,.2f} have no time of day and cannot be "
            "placed in a session; map a time column or derive the time from the reference in the import profile."
        )
    return table
