"""Idempotent demo data for the public project instance.

It never contacts the configured destinations. The rows merely make the dashboard legible on a
fresh SQLite file and are marked as fictional scenario data in the documentation.
"""

from __future__ import annotations

from .config import Settings
from .db import Database


def seed_demo_data(database: Database) -> None:
    if database.list_api_deliveries(limit=1, cursor=None):
        return
    scenarios = [
        ("invoice.paid", "https://billing.example.test/hooks/courier", {"invoice_id": "inv_demo_1042", "amount_cents": 4200}, "delivered", 202, None),
        ("shipment.dispatched", "https://ledger.example.test/hooks/courier", {"shipment_id": "shp_demo_001"}, "accepted", None, None),
        ("customer.address_updated", "https://crm.example.test/hooks/courier", {"customer_id": "cus_demo_018"}, "failed", None, "Fictional timeout retained as an observability example."),
    ]
    for event, destination, payload, state, response_status, error in scenarios:
        row = database.create_api_delivery(event=event, destination=destination, payload=payload)
        if state != "accepted":
            database.update_api_delivery(
                row["delivery_id"],
                status=state,
                response_status=response_status,
                error=error,
            )


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    seed_demo_data(database)
    print("Courier API demo data ready.")


if __name__ == "__main__":
    main()
