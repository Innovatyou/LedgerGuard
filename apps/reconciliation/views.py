from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView, View

from apps.accounting.models import Account
from apps.approvals.changes import request_change, requires_approval
from apps.approvals.services import history_for
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get
from apps.imports.models import SourceTransaction
from apps.suspense.services import create_from_transaction

from . import services
from .forms import ManualMatchForm, MatchingRuleForm, ReconciliationForm
from .models import MatchGroup, MatchingRule, Reconciliation, ReconExclusion
from .tasks import run_reconciliation_task


class RuleListView(InstitutionRequiredMixin, ListView):
    permission_required = "recon.view"
    template_name = "reconciliation/rule_list.html"

    def get_queryset(self):
        return MatchingRule.objects.filter(institution=self.institution)


class RuleEditView(InstitutionRequiredMixin, View):
    permission_required = "recon.rule_manage"

    def get(self, request, pk=None):
        rule = scoped_get(request, MatchingRule, pk=pk) if pk else None
        return render(request, "reconciliation/rule_form.html", {"form": MatchingRuleForm(instance=rule)})

    def post(self, request, pk=None):
        rule = scoped_get(request, MatchingRule, pk=pk) if pk else MatchingRule(institution=self.institution)
        before = rule.snapshot() if pk else None
        form = MatchingRuleForm(request.POST, instance=rule)
        if form.is_valid() and requires_approval(self.institution):
            try:
                cr = request_change(
                    self.institution,
                    request.user,
                    "matching_rule",
                    form.cleaned_data,
                    target_obj=rule if pk else None,
                    summary=f"{'Edit' if pk else 'Create'} matching rule {form.cleaned_data['name']}",
                )
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, f"Change {cr.reference} submitted for independent approval.")
                return redirect("approvals:changes")
        elif form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            log_activity(
                "recon.rule_saved", user=request.user, obj=obj, details={"before": before, "after": obj.snapshot()}
            )
            messages.success(request, "Matching rule saved.")
            return redirect("reconciliation:rules")
        return render(request, "reconciliation/rule_form.html", {"form": form})


class ReconListView(InstitutionRequiredMixin, ListView):
    permission_required = "recon.view"
    template_name = "reconciliation/recon_list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = Reconciliation.objects.filter(institution=self.institution).select_related("prepared_by")
        status = self.request.GET.get("status")
        return qs.filter(status=status) if status else qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["statuses"] = Reconciliation.Status.choices
        return ctx


class ReconCreateView(InstitutionRequiredMixin, View):
    permission_required = "recon.run"

    def get(self, request):
        initial = {}
        previous = None
        if request.GET.get("from"):
            previous = scoped_get(request, Reconciliation, pk=request.GET["from"])
            stmt = services.build_statement(previous)
            initial = {f: getattr(previous, f) for f in ReconciliationForm.Meta.fields if f != "rules"}
            initial.update(
                opening_left_balance=stmt.left.closing,
                opening_right_balance=stmt.right.closing,
                closing_left_balance=None,
                closing_right_balance=None,
                name=f"{previous.name} (next)",
            )
            initial["period_start"] = previous.period_end.fromordinal(previous.period_end.toordinal() + 1)
            initial["period_end"] = None
        form = ReconciliationForm(initial=initial, institution=self.institution)
        return render(request, "reconciliation/recon_form.html", {"form": form, "previous": previous})

    def post(self, request):
        form = ReconciliationForm(request.POST, institution=self.institution)
        if form.is_valid():
            data = dict(form.cleaned_data)
            data["rules"] = list(data.get("rules") or [])
            if request.POST.get("previous"):
                data["previous"] = scoped_get(request, Reconciliation, pk=request.POST["previous"])
            try:
                recon = services.create_reconciliation(self.institution, request.user, **data)
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, f"Reconciliation {recon.reference} created. Run it to match transactions.")
                return redirect("reconciliation:detail", pk=recon.pk)
        return render(request, "reconciliation/recon_form.html", {"form": form})


def _decorate(items, recon, side):
    return [(t, services.flow_of(recon, side, t)) for t in items]


class ReconDetailView(InstitutionRequiredMixin, View):
    permission_required = "recon.view"

    def get(self, request, pk):
        recon = scoped_get(request, Reconciliation, pk=pk)
        tab = request.GET.get("tab", "exceptions")
        ctx = {"recon": recon, "tab": tab, "history": history_for(recon)}
        stmt = services.build_statement(recon)
        ctx["statement"] = stmt
        ctx["validation_errors"] = stmt.validation_errors(recon.institution.reconciliation_tolerance)
        groups = recon.match_groups.prefetch_related("items__transaction").select_related("decided_by")
        if tab == "matches":
            status = request.GET.get("status", "ACCEPTED")
            ctx["group_page"] = Paginator(groups.filter(status=status), 50).get_page(request.GET.get("page"))
            ctx["group_status"] = status
        elif tab == "review":
            ctx["group_page"] = Paginator(groups.filter(status=MatchGroup.Status.PROPOSED), 50).get_page(
                request.GET.get("page")
            )
            ctx["group_status"] = "PROPOSED"
        ctx["suspense_accounts"] = Account.objects.filter(
            institution=self.institution, is_suspense_account=True, is_active=True
        )
        ctx["exclusions"] = recon.exclusions.select_related("transaction", "excluded_by")
        return render(request, "reconciliation/recon_detail.html", ctx)


class ReconActionView(InstitutionRequiredMixin, View):
    permission_required = "recon.view"

    def post(self, request, pk):
        recon = scoped_get(request, Reconciliation, pk=pk)
        action = request.POST.get("action")
        note = request.POST.get("note", "")
        back = reverse("reconciliation:detail", args=[recon.pk]) + "?tab=" + request.POST.get("tab", "exceptions")
        user = request.user

        if action == "run":
            in_scope = services.side_queryset(recon, "LEFT").count() + services.side_queryset(recon, "RIGHT").count()
            if in_scope > 20000:
                run_reconciliation_task.delay(recon.pk, user.pk)
                messages.info(request, "Large reconciliation queued for background processing.")
                return redirect(back)
            return run_action(request, lambda: services.run_reconciliation(recon, user), "Matching run complete.", back)
        if action in ("accept", "reject"):
            group = get_object_or_404(MatchGroup, pk=request.POST.get("group"), reconciliation=recon)
            func = services.accept_match if action == "accept" else services.reject_match
            return run_action(request, lambda: func(group, user, note), f"Match {action}ed.", back)
        if action == "accept_high_confidence":

            def accept_all():
                count = 0
                for g in recon.match_groups.filter(
                    status=MatchGroup.Status.PROPOSED, category=MatchGroup.Category.HIGH_CONFIDENCE, difference=0
                ):
                    services.accept_match(g, user, note or "Bulk accepted high-confidence matches")
                    count += 1
                return count

            return run_action(request, accept_all, "High-confidence matches accepted.", back)
        if action == "manual_match":
            form = ManualMatchForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Select transactions and enter a note.")
                return redirect(back)
            return run_action(
                request,
                lambda: services.manual_match(
                    recon, user, form.left_ids(), form.right_ids(), form.cleaned_data["note"]
                ),
                "Manual match recorded.",
                back,
            )
        if action == "exclude":
            return run_action(
                request,
                lambda: services.exclude_transaction(recon, user, int(request.POST["txn"]), note),
                "Transaction excluded.",
                back,
            )
        if action == "remove_exclusion":
            exclusion = get_object_or_404(ReconExclusion, pk=request.POST.get("exclusion"), reconciliation=recon)
            return run_action(request, lambda: services.remove_exclusion(exclusion, user), "Exclusion removed.", back)
        if action == "adjust":
            try:
                amount = Decimal(request.POST.get("amount", ""))
            except InvalidOperation:
                messages.error(request, "Enter a valid adjustment amount.")
                return redirect(back)
            return run_action(
                request,
                lambda: services.add_adjustment(
                    recon,
                    user,
                    side=request.POST.get("side"),
                    description=request.POST.get("description", ""),
                    amount=amount,
                    reference=request.POST.get("reference", ""),
                ),
                "Adjustment recorded.",
                back,
            )
        if action == "to_suspense":
            txn = get_object_or_404(SourceTransaction, pk=request.POST.get("txn"), institution=self.institution)
            account = get_object_or_404(Account, pk=request.POST.get("account"), institution=self.institution)
            return run_action(
                request,
                lambda: create_from_transaction(recon, txn, user, account=account, reason=note),
                "Suspense item created.",
                back,
            )
        if action == "submit":
            return run_action(
                request, lambda: services.submit_reconciliation(recon, user, note), "Submitted for approval.", back
            )
        if action == "approve":
            return run_action(
                request, lambda: services.approve_reconciliation(recon, user, note), "Reconciliation approved.", back
            )
        if action == "return":
            return run_action(
                request, lambda: services.return_reconciliation(recon, user, note), "Returned to preparer.", back
            )
        if action == "reopen":
            return run_action(
                request, lambda: services.reopen_reconciliation(recon, user, note), "Reconciliation reopened.", back
            )
        if action == "close":
            return run_action(
                request, lambda: services.close_reconciliation(recon, user, note), "Reconciliation closed.", back
            )
        messages.error(request, "Unknown action.")
        return redirect(back)
