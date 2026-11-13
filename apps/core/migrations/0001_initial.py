from django.db import migrations, models


def create_default_property(apps, schema_editor):
    Property = apps.get_model("core", "Property")
    Property.objects.get_or_create(
        code="DEFAULT",
        defaults={
            "name": "默认酒店",
            "timezone": "Asia/Shanghai",
            "currency": "CNY",
            "status": "ACTIVE",
        },
    )


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Property",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("code", models.CharField(max_length=32, unique=True, verbose_name="物业编码")),
                ("name", models.CharField(max_length=120, verbose_name="酒店名称")),
                ("timezone", models.CharField(default="Asia/Shanghai", max_length=64)),
                ("currency", models.CharField(default="CNY", max_length=3)),
                ("address", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("ACTIVE", "营业中"), ("INACTIVE", "停用")],
                        default="ACTIVE",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "酒店物业",
                "verbose_name_plural": "酒店物业",
                "ordering": ("code",),
            },
        ),
        migrations.RunPython(create_default_property, migrations.RunPython.noop),
    ]
