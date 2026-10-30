import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("core", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="Guest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("full_name", models.CharField(max_length=80)),
                ("phone", models.CharField(max_length=24)),
                ("phone_normalized", models.CharField(blank=True, max_length=24, null=True)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("notes", models.TextField(blank=True)),
                ("consent_version", models.CharField(blank=True, max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="guests", to="core.property")),
            ],
            options={"verbose_name": "住客档案", "verbose_name_plural": "住客档案", "ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="guest",
            constraint=models.UniqueConstraint(fields=("property", "phone_normalized"), name="uq_guest_property_phone"),
        ),
        migrations.AddIndex(
            model_name="guest",
            index=models.Index(fields=["property", "full_name"], name="guests_guest_propert_5f0a1d_idx"),
        ),
    ]
