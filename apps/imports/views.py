from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView, View

from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import services
from .forms import ImportProfileForm, UploadForm
from .models import STANDARD_FIELDS, ImportBatch, ImportProfile, ImportRow, SourceTransaction
from .tasks import validate_batch_task


class ProfileListView(InstitutionRequiredMixin, ListView):
    permission_required = "import.view"
    template_name = "imports/profile_list.html"

    def get_queryset(self):
        return ImportProfile.objects.filter(institution=self.institution)


class ProfileEditView(InstitutionRequiredMixin, View):
    permission_required = "import.profile_manage"

    def get(self, request, pk=None):
        profile = scoped_get(request, ImportProfile, pk=pk) if pk else None
        return render(request, "imports/profile_form.html", {"form": ImportProfileForm(instance=profile)})

    def post(self, request, pk=None):
        profile = (
            scoped_get(request, ImportProfile, pk=pk)
            if pk
            else ImportProfile(institution=self.institution, created_by=request.user)
        )
        form = ImportProfileForm(request.POST, instance=profile)
        if form.is_valid():
            obj = form.save(commit=False)
            try:
                services.validate_profile_mapping(obj)
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                obj.save()
                log_activity(
                    "import.profile_saved", user=request.user, obj=obj, details={"mapping": obj.column_mapping}
                )
                messages.success(request, "Import profile saved.")
                return redirect("imports:profiles")
        return render(request, "imports/profile_form.html", {"form": form})


class BatchListView(InstitutionRequiredMixin, ListView):
    permission_required = "import.view"
    template_name = "imports/batch_list.html"
    paginate_by = 50

    def get_queryset(self):
        return ImportBatch.objects.filter(institution=self.institution).select_related("profile", "uploaded_by")


class UploadView(InstitutionRequiredMixin, View):
    permission_required = "import.upload"

    def get(self, request):
        return render(request, "imports/upload.html", {"form": UploadForm(institution=self.institution)})

    def post(self, request):
        form = UploadForm(request.POST, request.FILES, institution=self.institution)
        if form.is_valid():
            upload = form.cleaned_data["file"]
            try:
                batch = services.create_batch(
                    self.institution, request.user, form.cleaned_data["profile"], upload.name, upload.read()
                )
            except BusinessRuleError as exc:
                form.add_error("file", exc.message)
            else:
                messages.success(request, "File uploaded. Review the preview and validate.")
                return redirect("imports:batch-detail", pk=batch.pk)
        return render(request, "imports/upload.html", {"form": form})


class BatchDetailView(InstitutionRequiredMixin, View):
    permission_required = "import.view"

    def get(self, request, pk):
        batch = scoped_get(request, ImportBatch, pk=pk)
        ctx = {"batch": batch, "standard_fields": STANDARD_FIELDS}
        if batch.status in (ImportBatch.Status.UPLOADED, ImportBatch.Status.FAILED) and batch.file:
            try:
                headers, rows, total = services.preview(batch)
                ctx.update(preview_headers=headers, preview_rows=rows, preview_total=total)
                mapped = set((batch.profile.column_mapping or {}).values())
                ctx["unmapped_headers"] = [h for h in headers if h not in mapped]
                ctx["missing_columns"] = [c for c in mapped if c not in headers]
            except BusinessRuleError as exc:
                ctx["preview_error"] = exc.message
        status = request.GET.get("status", "")
        rows = batch.rows.all()
        if status:
            rows = rows.filter(status=status)
        ctx["row_page"] = Paginator(rows, 100).get_page(request.GET.get("page"))
        ctx["row_status"] = status
        ctx["row_statuses"] = ImportRow.Status.choices
        return render(request, "imports/batch_detail.html", ctx)


class BatchActionView(InstitutionRequiredMixin, View):
    permission_required = "import.upload"

    def post(self, request, pk):
        batch = scoped_get(request, ImportBatch, pk=pk)
        action = request.POST.get("action")
        back = reverse("imports:batch-detail", args=[batch.pk])
        if action == "validate":
            if batch.size > 5 * 1024 * 1024:
                validate_batch_task.delay(batch.pk, request.user.pk)
                messages.info(request, "Validation queued; refresh this page shortly.")
                return redirect(back)
            return run_action(
                request, lambda: services.validate_batch(batch, request.user), "Validation complete.", back
            )
        if action == "commit":
            partial = request.POST.get("allow_partial") == "1"
            return run_action(
                request,
                lambda: services.commit_batch(batch, request.user, allow_partial=partial),
                "Batch committed.",
                back,
            )
        if action == "cancel":
            return run_action(
                request,
                lambda: services.cancel_batch(batch, request.user, request.POST.get("reason", "")),
                "Batch cancelled.",
                back,
            )
        row = get_object_or_404(batch.rows, pk=request.POST.get("row"))
        if action == "correct":
            changes = {k[4:]: v for k, v in request.POST.items() if k.startswith("col:")}
            changes = {k: v for k, v in changes.items() if (row.raw.get(k) or "") != v}
            return run_action(
                request,
                lambda: services.correct_row(row, request.user, changes, request.POST.get("note", "")),
                f"Row {row.row_number} corrected.",
                back,
            )
        if action in ("exclude", "include"):
            return run_action(
                request,
                lambda: services.set_row_excluded(
                    row, request.user, action == "exclude", request.POST.get("reason", "")
                ),
                f"Row {row.row_number} updated.",
                back,
            )
        messages.error(request, "Unknown action.")
        return redirect(back)


class TransactionListView(InstitutionRequiredMixin, ListView):
    permission_required = "import.view"
    template_name = "imports/transaction_list.html"
    paginate_by = 100

    def get_queryset(self):
        qs = SourceTransaction.objects.filter(institution=self.institution)
        params = self.request.GET
        if params.get("source_type"):
            qs = qs.filter(source_type=params["source_type"])
        if params.get("account_reference"):
            qs = qs.filter(account_reference=params["account_reference"])
        if params.get("batch"):
            qs = qs.filter(batch_id=params["batch"])
        if params.get("q"):
            q = params["q"]
            qs = qs.filter(transaction_id__icontains=q) | qs.filter(narration__icontains=q)
        return qs.order_by("-transaction_date", "-id")
