from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from apps.approvals.changes import request_change, requires_approval
from apps.approvals.services import history_for
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import recurring, services
from .coa_templates import TEMPLATE_WARNING, TEMPLATES, apply_template
from .forms import AccountForm, JournalForm, JournalLineFormSet, PeriodForm, RecurringJournalForm
from .models import Account, FinancialPeriod, Journal, RecurringJournal


def scope_journals(qs, scope):
    """Limit journals to those whose header branch is within the user's branch scope."""
    return qs if scope is None else qs.filter(branch_id__in=scope)


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- accounts


class AccountListView(InstitutionRequiredMixin, ListView):
    permission_required = "coa.view"
    template_name = "accounting/account_list.html"

    def get_queryset(self):
        qs = Account.objects.filter(institution=self.institution).select_related("parent")
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(name__icontains=q) | qs.filter(code__startswith=q)
        return qs.order_by("code")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        depth = {}
        for account in ctx["object_list"]:
            depth[account.pk] = depth.get(account.parent_id, -1) + 1 if account.parent_id else 0
            account.depth = depth[account.pk]
        ctx["templates"] = {k: v[0] for k, v in TEMPLATES.items()}
        ctx["template_warning"] = TEMPLATE_WARNING
        return ctx


class AccountEditView(InstitutionRequiredMixin, View):
    permission_required = "coa.manage"

    def get(self, request, pk=None):
        account = scoped_get(request, Account, pk=pk) if pk else None
        form = AccountForm(instance=account, institution=self.institution)
        return render(request, "accounting/account_form.html", {"form": form, "account": account})

    def post(self, request, pk=None):
        account = scoped_get(request, Account, pk=pk) if pk else None
        form = AccountForm(request.POST, instance=account, institution=self.institution)
        if form.is_valid() and requires_approval(self.institution):
            try:
                cr = request_change(
                    self.institution,
                    request.user,
                    "account",
                    form.cleaned_data,
                    target_obj=account,
                    summary=f"{'Edit' if account else 'Create'} account {form.cleaned_data['code']}",
                )
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, f"Change {cr.reference} submitted for independent approval.")
                return redirect("approvals:changes")
        elif form.is_valid():
            obj = form.save(commit=False)
            if not pk:
                obj.created_by = request.user
            try:
                obj.save()
            except ValidationError as exc:
                for field, errs in exc.message_dict.items():
                    form.add_error(None if field == "__all__" else field, errs)
            else:
                log_activity("coa.account_saved", user=request.user, obj=obj, details={"changed": form.changed_data})
                messages.success(request, f"Account {obj} saved.")
                return redirect("accounting:accounts")
        return render(request, "accounting/account_form.html", {"form": form, "account": account})


class ApplyTemplateView(InstitutionRequiredMixin, View):
    permission_required = "coa.manage"

    def post(self, request):
        key = request.POST.get("template")
        if key not in TEMPLATES:
            messages.error(request, "Unknown template.")
            return redirect("accounting:accounts")
        if requires_approval(self.institution) and Account.objects.filter(institution=self.institution).exists():
            messages.error(request, "Templates can only seed an empty chart while configuration approval is required.")
            return redirect("accounting:accounts")
        created = apply_template(self.institution, request.user, key)
        messages.success(request, f"{created} account(s) created. {TEMPLATE_WARNING}")
        return redirect("accounting:accounts")


# ---------------------------------------------------------------- periods


class PeriodListView(InstitutionRequiredMixin, ListView):
    permission_required = "journal.view"
    template_name = "accounting/period_list.html"

    def get_queryset(self):
        return FinancialPeriod.objects.filter(institution=self.institution).order_by("-start_date")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["form"] = PeriodForm()
        return ctx

    def post(self, request):
        action = request.POST.get("action")
        back = reverse("accounting:periods")
        if action == "create":
            form = PeriodForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Invalid period details.")
                return redirect(back)
            return run_action(
                request,
                lambda: services.create_period(self.institution, request.user, **form.cleaned_data),
                "Period created.",
                back,
            )
        if action == "generate":
            year = int(request.POST.get("year") or date.today().year)
            return run_action(
                request,
                lambda: services.generate_monthly_periods(self.institution, request.user, year),
                f"Monthly periods generated for financial year {year}.",
                back,
            )
        period = scoped_get(request, FinancialPeriod, pk=request.POST.get("period"))
        if action == "close":
            return run_action(request, lambda: services.close_period(period, request.user), f"{period} closed.", back)
        if action == "reopen":
            return run_action(
                request,
                lambda: services.reopen_period(period, request.user, request.POST.get("reason", "")),
                f"{period} reopened.",
                back,
            )
        messages.error(request, "Unknown action.")
        return redirect(back)


# ---------------------------------------------------------------- journals


class JournalListView(InstitutionRequiredMixin, ListView):
    permission_required = "journal.view"
    template_name = "accounting/journal_list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = scope_journals(Journal.objects.filter(institution=self.institution), self.branch_scope).select_related(
            "prepared_by", "period"
        )
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(number__icontains=q) | qs.filter(description__icontains=q)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["statuses"] = Journal.Status.choices
        return ctx


def _lines_from_formset(formset):
    lines = []
    for form in formset:
        if form.is_blank():
            continue
        data = form.cleaned_data
        if not data.get("account"):
            raise BusinessRuleError("Every line with an amount needs an account.")
        lines.append(
            services.LineInput(
                account=data["account"],
                debit=data.get("debit") or 0,
                credit=data.get("credit") or 0,
                description=data.get("description") or "",
                department=data.get("department"),
            )
        )
    return lines


class JournalScopeMixin:
    def _journal(self, pk):
        return get_object_or_404(
            scope_journals(Journal.objects.filter(institution=self.institution), self.branch_scope), pk=pk
        )


class JournalEditView(JournalScopeMixin, InstitutionRequiredMixin, View):
    permission_required = "journal.create"

    def _render(self, request, form, formset, journal=None):
        return render(request, "accounting/journal_form.html", {"form": form, "formset": formset, "journal": journal})

    def get(self, request, pk=None):
        journal = self._journal(pk) if pk else None
        if journal and not journal.is_editable:
            messages.error(request, "This journal can no longer be edited.")
            return redirect("accounting:journal-detail", pk=journal.pk)
        initial = []
        if journal:
            initial = [
                {
                    "account": ln.account,
                    "description": ln.description,
                    "debit": ln.debit or None,
                    "credit": ln.credit or None,
                    "department": ln.department,
                }
                for ln in journal.lines.all()
            ]
        form = JournalForm(instance=journal, institution=self.institution)
        formset = JournalLineFormSet(initial=initial, form_kwargs={"institution": self.institution})
        return self._render(request, form, formset, journal)

    def post(self, request, pk=None):
        journal = self._journal(pk) if pk else None
        form = JournalForm(request.POST, instance=journal, institution=self.institution)
        formset = JournalLineFormSet(request.POST, form_kwargs={"institution": self.institution})
        if form.is_valid() and formset.is_valid():
            fields = {k: form.cleaned_data[k] for k in form.Meta.fields}
            fields["accounting_date"] = fields["accounting_date"] or fields["transaction_date"]
            try:
                lines = _lines_from_formset(formset)
                if journal:
                    journal = services.update_journal(journal, request.user, lines=lines, **fields)
                else:
                    journal = services.create_journal(self.institution, request.user, lines=lines, **fields)
            except BusinessRuleError as exc:
                messages.error(request, exc.message)
            else:
                messages.success(request, "Journal saved as draft.")
                return redirect("accounting:journal-detail", pk=journal.pk)
        return self._render(request, form, formset, journal)


class JournalDetailView(InstitutionRequiredMixin, DetailView):
    permission_required = "journal.view"
    template_name = "accounting/journal_detail.html"
    context_object_name = "journal"

    def get_queryset(self):
        return scope_journals(Journal.objects.filter(institution=self.institution), self.branch_scope).select_related(
            "prepared_by", "approved_by", "posted_by", "period", "reversal_of"
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        journal = self.object
        ctx["lines"] = journal.lines.select_related("account", "department")
        ctx["totals"] = journal.totals()
        ctx["history"] = history_for(journal)
        ctx["reversal"] = Journal.objects.filter(reversal_of=journal).first()
        return ctx


class JournalActionView(JournalScopeMixin, InstitutionRequiredMixin, View):
    permission_required = "journal.view"

    ACTIONS = {
        "submit": (services.submit_journal, "Journal submitted for approval."),
        "approve": (services.approve_journal, "Journal approved."),
        "post": (services.post_journal, "Journal posted to the ledger."),
    }

    def post(self, request, pk):
        journal = self._journal(pk)
        action = request.POST.get("action")
        back = reverse("accounting:journal-detail", args=[journal.pk])
        comment = request.POST.get("comment", "")
        if action in self.ACTIONS:
            func, msg = self.ACTIONS[action]
            return run_action(request, lambda: func(journal, request.user, comment), msg, back)
        if action == "reject":
            return run_action(
                request,
                lambda: services.reject_journal(journal, request.user, comment),
                "Journal rejected and returned to the preparer.",
                back,
            )
        if action == "reverse":
            reversal_date = _parse_date(request.POST.get("reversal_date"))
            return run_action(
                request,
                lambda: services.request_reversal(journal, request.user, comment, reversal_date),
                "Reversal journal created and submitted for approval.",
                back,
                success_redirect=lambda rev: reverse("accounting:journal-detail", args=[rev.pk]),
            )
        raise PermissionDenied("Unknown action")


# ---------------------------------------------------------------- recurring journals


class RecurringJournalView(InstitutionRequiredMixin, View):
    permission_required = "journal.view"

    def get(self, request):
        return render(
            request,
            "accounting/recurring.html",
            {
                "templates": RecurringJournal.objects.filter(institution=self.institution).select_related(
                    "created_by", "approved_by"
                ),
                "form": RecurringJournalForm(),
                "formset": JournalLineFormSet(form_kwargs={"institution": self.institution}),
            },
        )

    def post(self, request):
        back = reverse("accounting:recurring")
        action = request.POST.get("action")
        if action == "create":
            form = RecurringJournalForm(request.POST)
            formset = JournalLineFormSet(request.POST, form_kwargs={"institution": self.institution})
            if not (form.is_valid() and formset.is_valid()):
                messages.error(request, "Check the template details and lines.")
                return redirect(back)
            return run_action(
                request,
                lambda: recurring.create_template(
                    self.institution, request.user, lines=_lines_from_formset(formset), **form.cleaned_data
                ),
                "Recurring journal created; it activates after independent approval.",
                back,
            )
        template = scoped_get(request, RecurringJournal, pk=request.POST.get("template"))
        if action == "approve":
            return run_action(
                request,
                lambda: recurring.approve_template(template, request.user),
                "Recurring journal approved and active.",
                back,
            )
        if action in ("pause", "resume"):
            return run_action(
                request,
                lambda: recurring.set_paused(template, request.user, action == "pause"),
                "Recurring journal updated.",
                back,
            )
        messages.error(request, "Unknown action.")
        return redirect(back)
