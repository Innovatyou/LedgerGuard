"""
Performance check: generate a bank statement and GL extract of N rows each for
a scratch institution, then time upload/validate/commit and a reconciliation run.
Never run against production data – it creates its own institution.
"""

import random
import time
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Membership, Role, User
from apps.imports import services as imports
from apps.imports.models import AmountMode, ImportProfile, Side, SourceType
from apps.organizations.models import Institution
from apps.reconciliation import services as recon
from apps.reconciliation.models import MatchingRule, Strategy


class Command(BaseCommand):
    help = "Time imports and reconciliation at volume in a scratch institution."

    def add_arguments(self, parser):
        parser.add_argument("--rows", type=int, default=50_000)
        parser.add_argument("--code", default="loadtest")

    def handle(self, *args, rows, code, **options):
        if Institution.objects.filter(code=code).exists():
            raise CommandError(f"Institution {code} exists; choose another --code.")
        inst = Institution.objects.create(code=code, name="Load test", institution_type="MFB")
        user = User.objects.create_user(f"{code}-user", f"{code}@example.invalid", "Load-Test-Passw0rd!")
        Membership.objects.create(
            user=user, institution=inst, role=Role.objects.get(institution=None, code="super_admin")
        )
        bank = ImportProfile.objects.create(
            institution=inst,
            name="bank",
            source_type=SourceType.BANK_STATEMENT,
            source_system="Bank",
            amount_mode=AmountMode.SEPARATE,
            default_account_reference="1201",
            date_formats=["%Y-%m-%d"],
            column_mapping={
                "transaction_date": "Date",
                "transaction_id": "Ref",
                "narration": "Narration",
                "debit_amount": "Debit",
                "credit_amount": "Credit",
            },
        )
        gl = ImportProfile.objects.create(
            institution=inst,
            name="gl",
            source_type=SourceType.GL_EXPORT,
            source_system="Core",
            amount_mode=AmountMode.SIGNED,
            signed_positive_is=Side.DEBIT,
            default_account_reference="1201",
            date_formats=["%Y-%m-%d"],
            column_mapping={
                "transaction_date": "Date",
                "transaction_id": "Ref",
                "narration": "Narration",
                "amount": "Amount",
            },
        )
        MatchingRule.objects.create(
            institution=inst,
            name="ref",
            strategy=Strategy.EXACT_REFERENCE,
            priority=10,
            auto_accept=True,
            date_tolerance_days=2,
        )
        MatchingRule.objects.create(
            institution=inst, name="amt", strategy=Strategy.AMOUNT_DATE, priority=20, date_tolerance_days=1
        )
        rnd = random.Random(42)  # noqa: S311 - reproducible synthetic test data, not security-related
        start = date(2026, 1, 1)
        bank_lines, gl_lines = ["Date,Ref,Narration,Debit,Credit"], ["Date,Ref,Narration,Amount"]
        for i in range(rows):
            day = start + timedelta(days=rnd.randrange(28))
            amount = Decimal(rnd.randrange(100, 5_000_000)) / 100
            inflow = rnd.random() < 0.5
            ref = f"TX{i:08d}"
            bank_lines.append(f"{day},{ref},Payment {i},{'' if inflow else amount},{amount if inflow else ''}")
            if rnd.random() < 0.97:  # 3% unmatched
                gl_ref = ref if rnd.random() < 0.9 else f"GL{i:08d}"  # 10% need amount/date matching
                gl_lines.append(f"{day},{gl_ref},Posting {i},{amount if inflow else -amount}")
        results = {}
        for label, profile, lines in (("bank", bank, bank_lines), ("gl", gl, gl_lines)):
            data = ("\n".join(lines) + "\n").encode()
            t0 = time.perf_counter()
            batch = imports.create_batch(inst, user, profile, f"{label}.csv", data)
            imports.validate_batch(batch, user)
            batch.refresh_from_db()
            t1 = time.perf_counter()
            imports.commit_batch(batch, user)
            t2 = time.perf_counter()
            results[label] = (len(lines) - 1, t1 - t0, t2 - t1)
            self.stdout.write(
                f"{label}: {len(lines) - 1} rows, {len(data) / 1e6:.1f} MB – "
                f"upload+validate {t1 - t0:.1f}s, commit {t2 - t1:.1f}s"
            )
        r = recon.create_reconciliation(
            inst,
            user,
            name="load",
            left_account_reference="1201",
            right_account_reference="1201",
            period_start=start,
            period_end=start + timedelta(days=30),
        )
        t0 = time.perf_counter()
        summary = recon.run_reconciliation(r, user)
        t1 = time.perf_counter()
        stmt = recon.build_statement(r)
        t2 = time.perf_counter()
        self.stdout.write(
            f"reconciliation run {t1 - t0:.1f}s, statement {t2 - t1:.1f}s – "
            f"auto {summary['auto_accepted']}, review {summary['high_confidence'] + summary['manual_review']}, "
            f"unmatched {summary['unmatched_left']}/{summary['unmatched_right']}, "
            f"difference {stmt.final_difference}"
        )
