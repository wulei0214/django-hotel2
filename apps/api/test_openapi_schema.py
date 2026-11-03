from django.test import TestCase


class PMS2OpenApiDocumentationTests(TestCase):
    def test_openapi_endpoint_is_openapi_31_and_contains_registered_business_apis(self):
        response = self.client.get("/api/v1/openapi.json")
        self.assertEqual(response.status_code, 200)
        spec = response.json()
        self.assertEqual(spec["openapi"], "3.1.0")
        paths = spec["paths"]
        expected = {
            "/api/v1/availability": {"get"},
            "/api/v1/reservations": {"post"},
            "/api/v1/reservations/{booking_no}": {"get"},
            "/api/v1/reservations/{booking_no}/cancel": {"post"},
            "/api/v1/front-desk/overview": {"get"},
            "/api/v1/front-desk/reservation-rooms/{reservation_room_id}/check-in": {"post"},
            "/api/v1/night-audit/preview": {"get"},
            "/api/v1/night-audit/run": {"post"},
            "/api/v1/guests": {"get"},
            "/api/v1/guests/create": {"post"},
            "/api/v1/approvals": {"get"},
            "/api/v1/audit-timeline": {"get"},
            "/api/v1/staff": {"get"},
            "/api/v1/memberships": {"get"},
            "/api/v1/openapi.json": {"get"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths)
            for method in methods:
                self.assertIn(method, paths[path])
        self.assertNotIn("/api/v1/login", paths)
        self.assertNotIn("/api/v1/register", paths)

    def test_public_booking_contract_uses_booking_access_token_not_phone(self):
        spec = self.client.get("/api/v1/openapi.json").json()
        detail = spec["paths"]["/api/v1/reservations/{booking_no}"]["get"]
        parameter_names = {item["name"] for item in detail["parameters"]}
        self.assertIn("X-Booking-Token", parameter_names)
        self.assertIn("token", parameter_names)
        self.assertNotIn("phone", parameter_names)
        create = spec["paths"]["/api/v1/reservations"]["post"]
        self.assertIn("access_token", create["description"].lower())

    def test_swagger_and_redoc_pages_are_available(self):
        swagger = self.client.get("/api/docs/")
        redoc = self.client.get("/api/redoc/")
        self.assertEqual(swagger.status_code, 200)
        self.assertContains(swagger, "SwaggerUIBundle")
        self.assertContains(swagger, "/api/v1/openapi.json")
        self.assertEqual(redoc.status_code, 200)
        self.assertContains(redoc, "redoc spec-url")
