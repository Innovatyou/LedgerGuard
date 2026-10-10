"""
Create a demonstration institution with users, chart of accounts, periods,
posted journals, imported bank/GL/settlement data, a reconciliation, suspense
items and an audit engagement. Intended for evaluation and training only.
"""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounting import services as acc
from apps.accounting.coa_templates import apply_template
from apps.accounting.models import Account
from apps.accounts.models import Membership, Role, User
from apps.audit import services as audit
from apps.imports import services as imp
from apps.imports.models import AmountMode, Channel, ImportProfile, Side, SourceType
from apps.organizations.models import Branch, Department, Institution, InstitutionType
from apps.reconciliation import services as rec
from apps.reconciliation.models import MatchingRule, Strategy
from apps.settlements.services import install_default_rules
from apps.suspense import services as sus
from apps.suspense.models import EscalationRule

D = Decimal

DEMO_USERS = [
    ("admin", "Amaka", "Admin", "institution_admin", "Institution Administrator"),
    ("cfo", "Chidi", "Okeke", "cfo", "Chief Financial Officer"),
    ("finance", "Funke", "Adeyemi", "finance_manager", "Finance Manager"),
    ("accountant", "Ade", "Bello", "accountant", "Accountant"),
    ("recon", "Rukayat", "Sani", "recon_officer", "Reconciliation Officer"),
    ("reviewer", "Emeka", "Nwosu", "recon_reviewer", "Reconciliation Reviewer"),
    ("settlement", "Segun", "Ojo", "settlement_officer", "Settlement Officer"),
    ("auditor", "Ibrahim", "Musa", "internal_auditor", "Internal Auditor"),
    ("extaudit", "Grace", "Eze", "external_auditor", "External Auditor"),
    ("compliance", "Kemi", "Lawal", "compliance_officer", "Compliance Officer"),
    ("viewer", "Board", "Viewer", "management_viewer", "Management Viewer"),
]


def csv(rows):
    return ("\n".join(",".join(str(c) for c in r) for r in rows) + "\n").encode()


class Command(BaseCommand):
    help = "Create a demonstration institution with realistic sample data."

    def add_arguments(self, parser):
        parser.add_argument("--code", default="demo-mfb")
        parser.add_argument("--password", help="Password for all demo users (default: random, printed once)")

    @transaction.atomic
    def handle(self, *args, code, password=None, **options):
        if Institution.objects.filter(code=code).exists():
            raise CommandError(f"Institution {code} already exists.")
        password = password or secrets.token_urlsafe(12)
        inst = Institution.objects.create(
            code=code,
            name="Demo Microfinance Bank Ltd",
            institution_type=InstitutionType.MFB,
            licence_number="CBN/MFB/DEMO/0001",
            address="12 Marina, Lagos Island, Lagos",
            reconciliation_tolerance=D("0.00"),
            backdate_limit_days=45,
        )
        hq = Branch.objects.create(institution=inst, code="001", name="Head Office", state="Lagos", is_head_office=True)
        Branch.objects.create(institution=inst, code="002", name="Ikeja Branch", state="Lagos")
        Branch.objects.create(institution=inst, code="003", name="Wuse Branch", state="FCT Abuja")
        Department.objects.create(institution=inst, code="FIN", name="Finance & Accounts", branch=hq)
        Department.objects.create(institution=inst, code="OPS", name="Operations", branch=hq)
        Department.objects.create(
            institution=inst, code="IT", name="Information Technology", kind=Department.Kind.COST_CENTRE, branch=hq
        )

        users = {}
        for username, first, last, role, title in DEMO_USERS:
            uname = f"{code}-{username}" if User.objects.filter(username=username).exists() else username
            user = User.objects.create_user(
                username=uname,
                email=f"{uname}@demo.ledgerguard.local",
                password=password,
                first_name=first,
                last_name=last,
                job_title=title,
            )
            Membership.objects.create(user=user, institution=inst, role=Role.objects.get(institution=None, code=role))
            users[username] = user

        today = timezone.localdate()
        apply_template(inst, users["finance"], "MFB")
        acc.generate_monthly_periods(inst, users["finance"], today.year - 1)
        acc.generate_monthly_periods(inst, users["finance"], today.year)
        a = {x.code: x for x in Account.objects.filter(institution=inst)}

        month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)  # previous month
        month_end = today.replace(day=1) - timedelta(days=1)

        def post(day, desc, lines):
            j = acc.create_journal(
                inst,
                users["accountant"],
                transaction_date=day,
                description=desc,
                lines=[acc.LineInput(account=a[c], debit=D(dr), credit=D(cr)) for c, dr, cr in lines],
            )
            acc.submit_journal(j, users["accountant"])
            acc.approve_journal(j, users["finance"])
            acc.post_journal(j, users["cfo"])
            return j

        opening = month_start - timedelta(days=1)
        post(
            opening,
            "Opening balances (migrated)",
            [
                ("1101", "15000000.00", "0"),
                ("1201", "85000000.00", "0"),
                ("1301", "420000000.00", "0"),
                ("1401", "60000000.00", "0"),
                ("1409", "0", "12000000.00"),
                ("1309", "0", "8400000.00"),
                ("2101", "0", "150000000.00"),
                ("2102", "0", "210000000.00"),
                ("2103", "0", "90000000.00"),
                ("3101", "0", "100000000.00"),
                ("3201", "0", "9600000.00"),
            ],
        )
        post(
            month_start + timedelta(days=4),
            "Interest on loans – accrual",
            [("1301", "6300000.00", "0"), ("4101", "0", "6300000.00")],
        )
        post(month_start + timedelta(days=9), "Salaries", [("5401", "4850000.00", "0"), ("1201", "0", "4850000.00")])
        post(
            month_start + timedelta(days=14), "Office rent", [("5601", "1250000.00", "0"), ("1201", "0", "1250000.00")]
        )
        post(
            month_start + timedelta(days=19),
            "Electronic channel fees",
            [("1201", "735000.00", "0"), ("4202", "0", "735000.00")],
        )
        post(month_end, "Depreciation", [("5501", "500000.00", "0"), ("1409", "0", "500000.00")])
        draft = acc.create_journal(
            inst,
            users["accountant"],
            transaction_date=today,
            description="Accrue audit fees",
            lines=[
                acc.LineInput(account=a["5602"], debit=D("750000")),
                acc.LineInput(account=a["2301"], credit=D("750000")),
            ],
        )
        acc.submit_journal(draft, users["accountant"])

        # Import profiles
        bank = ImportProfile.objects.create(
            institution=inst,
            name="Correspondent bank statement (CSV)",
            source_type=SourceType.BANK_STATEMENT,
            source_system="Correspondent bank",
            amount_mode=AmountMode.SEPARATE,
            date_formats=["%d/%m/%Y"],
            column_mapping={
                "transaction_date": "Txn Date",
                "value_date": "Value Date",
                "transaction_id": "Reference",
                "narration": "Description",
                "debit_amount": "Debit",
                "credit_amount": "Credit",
            },
            default_account_reference="1201",
            created_by=users["admin"],
        )
        gl = ImportProfile.objects.create(
            institution=inst,
            name="Core banking GL extract",
            source_type=SourceType.GL_EXPORT,
            source_system="Core banking",
            amount_mode=AmountMode.SIGNED,
            signed_positive_is=Side.DEBIT,
            date_formats=["%Y-%m-%d"],
            created_by=users["admin"],
            column_mapping={
                "transaction_date": "POST_DATE",
                "transaction_id": "TRAN_REF",
                "narration": "NARRATION",
                "amount": "AMOUNT",
                "account_reference": "GL_CODE",
            },
        )
        pos = ImportProfile.objects.create(
            institution=inst,
            name="POS settlement report",
            source_type=SourceType.SETTLEMENT_REPORT,
            source_system="Card processor",
            channel=Channel.POS,
            amount_mode=AmountMode.SEPARATE,
            date_formats=["%d/%m/%Y"],
            default_account_reference="1502",
            created_by=users["admin"],
            column_mapping={
                "transaction_date": "Txn Date",
                "transaction_id": "RRN",
                "settlement_batch_ref": "Batch",
                "gross_amount": "Gross",
                "fee_amount": "MSC",
                "net_amount": "Net",
                "credit_amount": "Net",
                "settlement_date": "Settle Date",
                "narration": "Merchant",
            },
        )

        f = lambda day: (month_start + timedelta(days=day)).strftime("%d/%m/%Y")  # noqa: E731
        g = lambda day: (month_start + timedelta(days=day)).isoformat()  # noqa: E731
        bank_rows = [
            ["Txn Date", "Value Date", "Reference", "Description", "Debit", "Credit"],
            [f(2), f(2), "NIP0001", "NIP TRF FROM ADEBAYO STORES", "", "2500000.00"],
            [f(5), f(5), "CHQ100231", "CHQ 100231 PRESENTED", "1250000.00", ""],
            [f(9), f(9), "SAL0925", "SALARY BATCH", "4850000.00", ""],
            [f(12), f(12), "NIP0002", "NIP TRF FROM OKONKWO & SONS", "", "780000.00"],
            [f(15), f(15), "FEE0915", "CHANNEL FEES SETTLEMENT", "", "735000.00"],
            [f(20), f(20), "UNKNOWN99", "TRF FROM UNKNOWN REMITTER", "", "450000.00"],
            [f(27), f(27), "SMSCHG", "SMS ALERT CHARGES", "18275.00", ""],
            [f(27), f(27), "SMSCHG", "SMS ALERT CHARGES", "18275.00", ""],
            [
                month_end.strftime("%d/%m/%Y"),
                month_end.strftime("%d/%m/%Y"),
                "COT",
                "COMMISSION ON TURNOVER",
                "5375.00",
                "",
            ],
        ]
        gl_rows = [
            ["POST_DATE", "TRAN_REF", "NARRATION", "AMOUNT", "GL_CODE"],
            [g(2), "NIP0001", "Inward NIP Adebayo Stores", "2500000.00", "1201"],
            [g(4), "CHQ100231", "Rent cheque 100231", "-1250000.00", "1201"],
            [g(9), "SAL0925", "Salaries", "-4850000.00", "1201"],
            [g(13), "RCPT-4471", "Receipt Okonkwo & Sons", "780000.00", "1201"],
            [g(15), "FEE0915", "Channel fee income", "735000.00", "1201"],
            [g(28), "CHQ100245", "Cheque 100245 to vendor", "-320000.00", "1201"],
            [month_end.isoformat(), "DEP-889", "Cash deposit in transit", "1100000.00", "1201"],
        ]
        pos_rows = [["Txn Date", "RRN", "Merchant", "Batch", "Gross", "MSC", "Net", "Settle Date"]]
        for i in range(5):
            pos_rows.append(
                [f(18), f"RRN{1000 + i}", "SHOPRITE LEKKI", "SB-POS-18", "200000.00", "1500.00", "198500.00", f(19)]
            )
        pos_rows.append([f(18), "RRN1099", "SPAR IKEJA", "SB-POS-18", "50000.00", "375.00", "49600.00", f(19)])

        recon_user = users["recon"]
        imp_batch = imp.create_batch(inst, recon_user, bank, f"statement_{month_start:%Y%m}.csv", csv(bank_rows))
        imp.validate_batch(imp_batch, recon_user)
        imp.commit_batch(imp_batch, recon_user)
        gl_batch = imp.create_batch(inst, recon_user, gl, f"gl_1201_{month_start:%Y%m}.csv", csv(gl_rows))
        imp.validate_batch(gl_batch, recon_user)
        imp.commit_batch(gl_batch, recon_user)
        pos_batch = imp.create_batch(inst, users["settlement"], pos, f"pos_{month_start:%Y%m}.csv", csv(pos_rows))
        imp.validate_batch(pos_batch, users["settlement"])
        imp.commit_batch(pos_batch, users["settlement"])

        MatchingRule.objects.create(
            institution=inst,
            name="Exact reference & amount",
            strategy=Strategy.EXACT_REFERENCE,
            priority=10,
            date_tolerance_days=3,
            auto_accept=True,
            updated_by=users["admin"],
        )
        MatchingRule.objects.create(
            institution=inst,
            name="Amount within 3 days",
            strategy=Strategy.AMOUNT_DATE,
            priority=50,
            date_tolerance_days=3,
            updated_by=users["admin"],
        )
        MatchingRule.objects.create(
            institution=inst,
            name="Similar narration (review)",
            strategy=Strategy.NARRATION_SIMILARITY,
            priority=90,
            date_tolerance_days=5,
            similarity_threshold=D("0.80"),
            updated_by=users["admin"],
        )
        install_default_rules(inst, users["admin"])

        bank_opening, gl_opening = D("85000000.00"), D("85000000.00")
        recon = rec.create_reconciliation(
            inst,
            recon_user,
            name=f"Correspondent bank current account – {month_start:%B %Y}",
            left_account_reference="1201",
            right_account_reference="1201",
            gl_account=a["1201"],
            period_start=month_start,
            period_end=month_end,
            opening_left_balance=bank_opening,
            opening_right_balance=gl_opening,
            closing_left_balance=D("83323075.00"),
            closing_right_balance=D("83695000.00"),
        )
        rec.run_reconciliation(recon, recon_user)

        EscalationRule.objects.create(institution=inst, name="Over 30 days", min_age_days=30)
        EscalationRule.objects.create(
            institution=inst, name="Over 90 days (CFO)", min_age_days=90, notify_permission="report.sensitive"
        )
        sus.create_item(
            inst,
            recon_user,
            account=a["2390"],
            original_date=month_start + timedelta(days=20),
            original_amount=D("450000.00"),
            side="CREDIT",
            transaction_reference="UNKNOWN99",
            reason="Inward transfer with no identifiable beneficiary",
            source_system="Correspondent bank",
        )
        sus.create_item(
            inst,
            recon_user,
            account=a["1590"],
            original_date=today - timedelta(days=97),
            original_amount=D("1275000.00"),
            side="DEBIT",
            transaction_reference="ATM-RVSL-2231",
            reason="ATM dispense error awaiting switch reversal",
            assigned_to=recon_user,
            source_system="ATM switch",
        )

        eng = audit.create_engagement(
            inst,
            users["auditor"],
            title="Review of bank reconciliation controls",
            kind="INTERNAL",
            scope="Timeliness, completeness and approval of bank reconciliations; suspense account management.",
            period_start=month_start - timedelta(days=60),
            period_end=month_end,
            lead=users["auditor"],
        )
        audit.create_workpaper(
            eng,
            users["auditor"],
            reference="WP-01",
            title="Reconciliation approval testing",
            objective="Confirm independent approval of reconciliations",
        )
        audit.raise_finding(
            eng,
            users["auditor"],
            title="Suspense items older than 90 days",
            rating="HIGH",
            condition="ATM dispense error of ₦1,275,000 outstanding for over 90 days.",
            criteria="Suspense policy requires resolution within 30 days.",
            recommendation="Escalate to the switch provider and resolve or provide.",
            owner=users["finance"],
        )

        self.stdout.write(self.style.SUCCESS(f"Demo institution '{inst.name}' created (code {code})."))
        self.stdout.write("Users: " + ", ".join(u.username for u in users.values()))
        self.stdout.write(f"Password for all demo users: {password}")
        self.stdout.write("Change these credentials before any shared use.")
