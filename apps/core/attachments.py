"""Evidence attachments for controlled records (journals, reconciliations, suspense items, audit records)."""

import os

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.accounts.services import has_perm

from .activity import log_activity
from .models import Attachment
from .views_mixins import InstitutionRequiredMixin

# label -> (model, permission to view, permission to attach)
TARGETS = {
    "journal": ("accounting.Journal", "journal.view", "journal.create"),
    "reconciliation": ("reconciliation.Reconciliation", "recon.view", "recon.investigate"),
    "suspense": ("suspense.SuspenseItem", "suspense.view", "suspense.manage"),
    "engagement": ("audit.Engagement", "audit.view", "audit.manage"),
    "finding": ("audit.Finding", "audit.view", "audit.respond"),
}

ALLOWED_EVIDENCE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".csv", ".docx", ".txt", ".msg", ".eml"}
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024


def _target(institution, label, object_id):
    if label not in TARGETS:
        raise Http404
    model = apps.get_model(TARGETS[label][0])
    return get_object_or_404(model, pk=object_id, institution=institution)


def attachments_for(obj, label):
    return Attachment.objects.filter(institution=obj.institution, content_type_label=label, object_id=str(obj.pk))


class AttachmentUploadView(InstitutionRequiredMixin, View):
    def post(self, request, label, object_id):
        obj = _target(self.institution, label, object_id)
        if not has_perm(request.user, self.institution, TARGETS[label][2]):
            raise PermissionDenied
        back = request.POST.get("next", "/")
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
            back = "/"
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose a file to attach.")
            return redirect(back)
        ext = os.path.splitext(upload.name)[1].lower()
        if ext not in ALLOWED_EVIDENCE_EXTENSIONS or upload.size > MAX_EVIDENCE_BYTES:
            messages.error(request, "File type not permitted or file larger than 20 MB.")
            return redirect(back)
        attachment = Attachment(
            institution=self.institution,
            original_name=os.path.basename(upload.name)[:255],
            size=upload.size,
            description=request.POST.get("description", "")[:255],
            content_type_label=label,
            object_id=str(obj.pk),
            uploaded_by=request.user,
            sha256=Attachment.checksum(upload),
        )
        attachment.file.save(upload.name, upload, save=False)
        attachment.save()
        log_activity(
            "evidence.attached",
            user=request.user,
            obj=obj,
            details={"file": attachment.original_name, "sha256": attachment.sha256},
        )
        messages.success(request, "Evidence attached.")
        return redirect(back)


class AttachmentDownloadView(InstitutionRequiredMixin, View):
    def get(self, request, pk):
        attachment = get_object_or_404(Attachment, pk=pk, institution=self.institution)
        label = attachment.content_type_label
        if label not in TARGETS or not has_perm(request.user, self.institution, TARGETS[label][1]):
            raise PermissionDenied
        log_activity(
            "evidence.downloaded",
            user=request.user,
            institution=self.institution,
            details={"attachment": attachment.pk, "file": attachment.original_name},
        )
        return FileResponse(attachment.file.open("rb"), as_attachment=True, filename=attachment.original_name)
