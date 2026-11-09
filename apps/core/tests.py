from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import Property


class PropertyModelTests(TestCase):
    def test_property_code_is_unique(self):
        Property.objects.create(code="P01", name="测试酒店")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Property.objects.create(code="P01", name="重复酒店")

    def test_default_property_is_seeded(self):
        self.assertTrue(Property.objects.filter(code="DEFAULT", status="ACTIVE").exists())
