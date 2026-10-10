from django.db import models, transaction


class DocumentSequence(models.Model):
    """Gap-free, per-institution document numbering (journals, reconciliations ...)."""

    institution = models.ForeignKey("organizations.Institution", on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=40)
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        app_label = "core"
        constraints = [models.UniqueConstraint(fields=["institution", "key"], name="uniq_sequence_key")]


def next_number(institution, key: str, prefix: str, width: int = 6) -> str:
    with transaction.atomic():
        seq, _ = DocumentSequence.objects.get_or_create(institution=institution, key=key)
        seq = DocumentSequence.objects.select_for_update().get(pk=seq.pk)
        value = seq.next_value
        seq.next_value = value + 1
        seq.save(update_fields=["next_value"])
    return f"{prefix}{str(value).zfill(width)}"
