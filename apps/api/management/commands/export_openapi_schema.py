"""Export the runtime OpenAPI contract to the committed documentation snapshot."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from apps.api.openapi_schema import build_openapi


class Command(BaseCommand):
    help = "Regenerate docs/接口文档/openapi.json from the current runtime API contract."

    def handle(self, *args, **options):
        output = Path(settings.BASE_DIR) / "docs" / "接口文档" / "openapi.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        spec = build_openapi()
        output.write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"已生成 OpenAPI 快照：{output}（{len(spec.get('paths', {}))} paths）"))
