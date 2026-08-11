from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from courier_api.config import Settings
from courier_api.main import create_app
from courier_api.security import sign_webhook_payload


API_KEY = "test_local_key"


def shipment_payload(reference: str = "ORDER-DEMO-001") -> dict:
    return {
        "reference": reference,
        "recipient": {
            "name": "Maya N'Diaye",
            "email": "maya@example.test",
            "phone": "+33601020304",
            "address_line1": "12 rue des Lilas",
            "postal_code": "75011",
            "city": "Paris",
            "country_code": "fr",
        },
        "service_level": "express",
        "parcels": [{"weight_grams": 850, "length_cm": 20, "width_cm": 15, "height_cm": 8}],
        "metadata": {"source": "test"},
    }


class CourierApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        settings = Settings(
            database_url=f"sqlite:///{self.tempdir.name}/courier.db",
            api_keys=f"local:{API_KEY}",
            allow_insecure_webhooks=False,
            webhook_timeout_seconds=0.1,
            rate_limit_per_minute=10_000,
            enable_demo_keys=True,
        )
        self.app = create_app(settings)
        self.client = TestClient(self.app)
        self.headers = {"X-API-Key": API_KEY}

        async def skip_outbound(_: str) -> None:
            return None

        self.app.state.dispatcher.dispatch_api_delivery = skip_outbound

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_shipment(self, reference: str, key: str) -> dict:
        response = self.client.post(
            "/v1/shipments",
            headers={**self.headers, "Idempotency-Key": key},
            json=shipment_payload(reference),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_health_openapi_and_authentication(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Courier", home.text)

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["service"], "courier-api")
        self.assertTrue(health.headers["X-Request-ID"].startswith("req_"))

        openapi = self.client.get("/openapi.json")
        self.assertEqual(openapi.status_code, 200)
        self.assertIn("/v1/shipments", openapi.json()["paths"])
        self.assertIn("/v1/deliveries", openapi.json()["paths"])

        unauthorized = self.client.get("/v1/shipments")
        self.assertEqual(unauthorized.status_code, 401)

    def test_raw_render_secret_is_a_valid_key_configuration(self) -> None:
        settings = Settings(api_keys="a-render-generated-secret")
        self.assertEqual(settings.parsed_api_keys, {"render": "a-render-generated-secret"})

    def test_shipment_idempotency_and_status_transitions(self) -> None:
        key = "idem-shipment-0001"
        first = self.client.post(
            "/v1/shipments",
            headers={**self.headers, "Idempotency-Key": key},
            json=shipment_payload(),
        )
        self.assertEqual(first.status_code, 201, first.text)
        shipment = first.json()
        self.assertEqual(shipment["recipient"]["country_code"], "FR")

        replay = self.client.post(
            "/v1/shipments",
            headers={**self.headers, "Idempotency-Key": key},
            json=shipment_payload(),
        )
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json()["id"], shipment["id"])

        conflict = self.client.post(
            "/v1/shipments",
            headers={**self.headers, "Idempotency-Key": key},
            json=shipment_payload("ORDER-CHANGED"),
        )
        self.assertEqual(conflict.status_code, 409)

        invalid_transition = self.client.patch(
            f"/v1/shipments/{shipment['id']}/status",
            headers={**self.headers, "Idempotency-Key": "idem-status-0001"},
            json={"status": "delivered"},
        )
        self.assertEqual(invalid_transition.status_code, 409)

        labelled = self.client.patch(
            f"/v1/shipments/{shipment['id']}/status",
            headers={**self.headers, "Idempotency-Key": "idem-status-0002"},
            json={"status": "labelled", "note": "Label printed"},
        )
        self.assertEqual(labelled.status_code, 200, labelled.text)
        self.assertEqual(labelled.json()["status"], "labelled")

    def test_cursor_pagination_and_audit_log(self) -> None:
        for number in range(3):
            self.create_shipment(f"ORDER-{number}", f"idem-page-{number:04d}")

        first_page = self.client.get("/v1/shipments?limit=2", headers=self.headers)
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(len(first_page.json()["data"]), 2)
        self.assertIsNotNone(first_page.json()["next_cursor"])

        second_page = self.client.get(
            "/v1/shipments",
            params={"limit": 2, "cursor": first_page.json()["next_cursor"]},
            headers=self.headers,
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(len(second_page.json()["data"]), 1)

        audit = self.client.get("/v1/audit-events?limit=20", headers=self.headers)
        self.assertEqual(audit.status_code, 200)
        self.assertTrue(any(item["path"] == "/v1/shipments" for item in audit.json()["data"]))

    def test_demo_key_bearer_delivery_and_public_overview(self) -> None:
        issued = self.client.post("/api/demo", json={"label": "tests", "ttl_minutes": 1})
        self.assertEqual(issued.status_code, 201, issued.text)
        demo_key = issued.json()["key"]
        self.assertTrue(demo_key.startswith("demo_crr_"))

        forbidden = self.client.get(
            "/v1/shipments", headers={"Authorization": f"Bearer {demo_key}"}
        )
        self.assertEqual(forbidden.status_code, 403)

        created = self.client.post(
            "/v1/deliveries",
            headers={"Authorization": f"Bearer {demo_key}", "Idempotency-Key": "idem-delivery-001"},
            json={
                "event": "invoice.paid",
                "destination": "https://billing.example.test/hooks/courier",
                "payload": {"invoice_id": "inv_1042", "amount_cents": 4200},
            },
        )
        self.assertEqual(created.status_code, 202, created.text)
        self.assertTrue(created.json()["trace_id"].startswith("trc_"))

        duplicate = self.client.post(
            "/v1/deliveries",
            headers={"Authorization": f"Bearer {demo_key}", "Idempotency-Key": "idem-delivery-001"},
            json={
                "event": "invoice.paid",
                "destination": "https://billing.example.test/hooks/courier",
                "payload": {"invoice_id": "inv_1042", "amount_cents": 4200},
            },
        )
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(duplicate.headers["Idempotency-Replayed"], "true")

        deliveries = self.client.get(
            "/v1/deliveries", headers={"Authorization": f"Bearer {demo_key}"}
        )
        self.assertEqual(deliveries.status_code, 200)
        self.assertEqual(len(deliveries.json()["items"]), 1)

        overview = self.client.get("/api/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["deliveries"][0]["destination"], "https://billing.example.test/…")

    def test_webhook_signature_is_deterministic(self) -> None:
        signature = sign_webhook_payload(
            secret="a" * 32,
            timestamp="2026-08-11T10:00:00+00:00",
            payload=b'{"id":"evt_1"}',
        )
        self.assertEqual(
            signature,
            "v1=4d0e27c350df0c4ba7550fed00cbd8375c93ef9263c7f6afc85a5c7a9019e33a",
        )


if __name__ == "__main__":
    unittest.main()
