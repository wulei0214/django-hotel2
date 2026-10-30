from django.db import models


class Guest(models.Model):
    property = models.ForeignKey(
        "core.Property", on_delete=models.PROTECT, related_name="guests"
    )
    full_name = models.CharField(max_length=80)
    phone = models.CharField(max_length=24)
    phone_normalized = models.CharField(max_length=24, null=True, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    consent_version = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("property", "phone_normalized"),
                name="uq_guest_property_phone",
            )
        ]
        indexes = [
            models.Index(fields=("property", "full_name"), name="guests_guest_propert_5f0a1d_idx"),
        ]
        verbose_name = "住客档案"
        verbose_name_plural = "住客档案"

    def __str__(self):
        return f"{self.full_name} ({self.phone})"
