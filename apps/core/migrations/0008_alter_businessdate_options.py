# -*- coding: utf-8 -*-
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0007_break_glass_record")]

    operations = [
        migrations.AlterModelOptions(
            name="businessdate",
            options={
                "verbose_name": "营业日",
                "verbose_name_plural": "营业日",
                "permissions": (("breakglass_review", "可以复核紧急营业日重开"),),
            },
        ),
    ]
