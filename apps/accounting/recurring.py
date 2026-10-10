"""Recurring journal templates: maker-checker activation and scheduled generation."""

import calendar
import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts.services import require_perm
from apps.approvals.services import assert_independent
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError

from . import services
from .models import Account, Journal, RecurringJournal

logger = logging.getLogger(__name__)
_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "ANNUALLY": 12}


def _clamp(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def add_months(day: date, months: int, day_of_month: int) -> date:
    index = day.month - 1 + months
    return _clamp(day.year + index // 12, index % 12 + 1, day_of_month)


def first_run(start: date, day_of_month: int) -> date:
    candidate = _clamp(start.year, start.month, day_of_month)
    return candidate if candidate >= start else add_months(candidate, 1, day_of_month)


def _line_inputs(template: RecurringJournal):
    accounts = {
        a.pk: a
        for a in Account.objects.filter(
            institution=template.institution, pk__in=[ln["account"] for ln in template.lines]
        )
    }
    lines = []
    for ln in template.lines:
        account = accounts.get(int(ln["account"]))
        if account is None:
            raise BusinessRuleError("A template line refers to an account of another institution or a deleted account.")
        lines.append(
            services.LineInput(
                account=account,
                debit=Decimal(str(ln.get("debit") or "0")),
                credit=Decimal(str(ln.get("credit") or "0")),
                description=ln.get("description", ""),
            )
        )
    return lines


def create_template(
    institution,
    user,
    *,
    name,
    description,
    lines,
    start_date,
    frequency="MONTHLY",
    day_of_month=28,
    end_date=None,
    currency=None,
    journal_type="ACCRUAL",
    branch=None,
):
    require_perm(user, institution, "journal.recurring_manage")
    if not 1 <= int(day_of_month) <= 31:
        raise BusinessRuleError("Day of month must be between 1 and 31.")
    serialised = [
        {"account": ln.account.pk, "debit": str(ln.debit), "credit": str(ln.credit), "description": ln.description}
        for ln in lines
    ]
    template = RecurringJournal(
        institution=institution,
        name=name,
        description=description,
        lines=serialised,
        start_date=start_date,
        frequency=frequency,
        day_of_month=day_of_month,
        end_date=end_date,
        journal_type=journal_type,
        currency=(currency or institution.base_currency).upper(),
        branch=branch,
        created_by=user,
        next_run_date=first_run(start_date, int(day_of_month)),
    )
    # Validate the lines now by building (not saving) a journal in a rolled-back transaction.
    try:
        with transaction.atomic():
            journal = services.create_journal(
                institution,
                user,
                transaction_date=start_date,
                description=description,
                lines=_line_inputs(template),
                currency=template.currency,
            )
            services.validate_journal(journal)
            raise _Rollback
    except _Rollback:
        pass
    template.save()
    log_activity("recurring.created", user=user, obj=template, details={"lines": serialised})
    return template


class _Rollback(Exception):
    pass


def approve_template(template, user):
    require_perm(user, template.institution, "journal.approve")
    if template.status != RecurringJournal.Status.PENDING:
        raise BusinessRuleError("Only templates awaiting approval can be approved.")
    assert_independent(template.institution, template.created_by, user, "this recurring journal")
    template.status = RecurringJournal.Status.ACTIVE
    template.approved_by = user
    template.approved_at = timezone.now()
    template.save()
    log_activity("recurring.approved", user=user, obj=template)
    return template


def set_paused(template, user, paused: bool):
    require_perm(user, template.institution, "journal.recurring_manage")
    if paused and template.status == RecurringJournal.Status.ACTIVE:
        template.status = RecurringJournal.Status.PAUSED
    elif not paused and template.status == RecurringJournal.Status.PAUSED:
        template.status = RecurringJournal.Status.ACTIVE
    else:
        raise BusinessRuleError("Template is not in a state that allows this.")
    template.save()
    log_activity("recurring.paused" if paused else "recurring.resumed", user=user, obj=template)
    return template


def generate_due(institution, today: date | None = None) -> list[Journal]:
    """Generate journals for every active template due on or before ``today``; idempotent per run date."""
    today = today or timezone.localdate()
    generated = []
    for template in RecurringJournal.objects.filter(
        institution=institution, status=RecurringJournal.Status.ACTIVE, next_run_date__lte=today
    ):
        while template.status == RecurringJournal.Status.ACTIVE and template.next_run_date <= today:
            run_date = template.next_run_date
            if template.end_date and run_date > template.end_date:
                template.status = RecurringJournal.Status.ENDED
                break
            try:
                with transaction.atomic():
                    journal = services.create_journal(
                        institution,
                        template.created_by,
                        transaction_date=run_date,
                        accounting_date=run_date,
                        description=f"{template.description} ({run_date:%b %Y})",
                        lines=_line_inputs(template),
                        currency=template.currency,
                        journal_type="RECURRING",
                        branch=template.branch,
                        source_reference=f"RJ-{template.pk}",
                        idempotency_key=f"recurring-{template.pk}-{run_date}",
                    )
                    if journal.status == Journal.Status.DRAFT:
                        journal = services.submit_journal(
                            journal, template.created_by, "Generated from recurring template"
                        )
                generated.append(journal)
                template.last_error = ""
            except BusinessRuleError as exc:
                template.last_error = f"{run_date}: {exc.message}"
                logger.warning("Recurring journal %s failed for %s: %s", template.pk, run_date, exc.message)
                log_activity(
                    "recurring.failed",
                    institution=institution,
                    obj=template,
                    details={"run_date": run_date, "error": exc.message},
                )
                break
            template.last_run_date = run_date
            template.next_run_date = add_months(run_date, _MONTHS[template.frequency], template.day_of_month)
        template.save()
    return generated
