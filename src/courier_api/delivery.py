"""Signed outbound webhook delivery with a durable attempt log."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .db import Database
from .security import sign_webhook_payload


class WebhookDispatcher:
    def __init__(self, database: Database, timeout_seconds: float) -> None:
        self.database = database
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def event_envelope(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event["id"],
            "type": event["type"],
            "occurred_at": event["created_at"],
            "data": event["payload"],
        }

    async def dispatch(self, event: dict[str, Any]) -> None:
        envelope = self.event_envelope(event)
        jobs = []
        for endpoint in self.database.endpoints_for_event(event["type"]):
            delivery = self.database.create_delivery(
                endpoint_id=endpoint["id"],
                event_id=event["id"],
                event_type=event["type"],
                payload=envelope,
                attempt=1,
            )
            jobs.append(self._deliver(endpoint, delivery))
        if jobs:
            await asyncio.gather(*jobs)

    async def retry(self, endpoint_id: str, delivery_id: str) -> dict[str, Any] | None:
        previous = self.database.get_delivery(delivery_id)
        endpoint = self.database.get_webhook_endpoint(endpoint_id)
        if not previous or previous["endpoint_id"] != endpoint_id or not endpoint:
            return None
        delivery = self.database.create_delivery(
            endpoint_id=endpoint_id,
            event_id=previous["event_id"],
            event_type=previous["event_type"],
            payload=previous["payload"],
            attempt=previous["attempt"] + 1,
        )
        await self._deliver(endpoint, delivery)
        return self.database.get_delivery(delivery["id"])

    async def dispatch_api_delivery(self, delivery_id: str) -> None:
        """Attempt a one-off outbound call accepted through /v1/deliveries.

        Registered webhook endpoints are the signed, retryable integration path. This endpoint is
        deliberately a compact playground surface: it records the request and makes one bounded
        attempt, leaving an audit-friendly trace either way.
        """
        delivery = self.database.get_api_delivery(delivery_id)
        if not delivery:
            return
        self.database.update_api_delivery(
            delivery_id, status="queued", response_status=None, error=None
        )
        envelope = json.dumps(
            {
                "id": delivery["delivery_id"],
                "trace_id": delivery["trace_id"],
                "type": delivery["event"],
                "data": delivery["payload"],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            status_code = await asyncio.to_thread(
                self._post,
                delivery["destination"],
                envelope,
                {
                    "Content-Type": "application/json",
                    "User-Agent": "Courier-API/0.1",
                    "X-Courier-Delivery": delivery["delivery_id"],
                    "X-Courier-Trace": delivery["trace_id"],
                    "X-Courier-Event": delivery["event"],
                },
            )
            outcome = "delivered" if 200 <= status_code < 300 else "failed"
            self.database.update_api_delivery(
                delivery_id,
                status=outcome,
                response_status=status_code,
                error=None if outcome == "delivered" else f"Receiver returned HTTP {status_code}.",
            )
        except Exception as error:
            self.database.update_api_delivery(
                delivery_id,
                status="failed",
                response_status=None,
                error=str(error)[:300],
            )

    async def _deliver(self, endpoint: dict[str, Any], delivery: dict[str, Any]) -> None:
        payload = json.dumps(delivery["payload"], separators=(",", ":")).encode("utf-8")
        timestamp = datetime.now(UTC).isoformat()
        signature = sign_webhook_payload(
            secret=endpoint["secret"], timestamp=timestamp, payload=payload
        )
        try:
            status_code = await asyncio.to_thread(
                self._post,
                endpoint["url"],
                payload,
                {
                    "Content-Type": "application/json",
                    "User-Agent": "Courier-API/0.1",
                    "X-Courier-Event": delivery["event_type"],
                    "X-Courier-Delivery": delivery["id"],
                    "X-Courier-Timestamp": timestamp,
                    "X-Courier-Signature": signature,
                },
            )
            outcome = "delivered" if 200 <= status_code < 300 else "failed"
            self.database.complete_delivery(
                delivery["id"],
                status=outcome,
                response_status=status_code,
                error=None if outcome == "delivered" else f"Receiver returned HTTP {status_code}.",
            )
        except Exception as error:  # The delivery log is the source of truth for transient faults.
            self.database.complete_delivery(
                delivery["id"],
                status="failed",
                response_status=None,
                error=str(error)[:300],
            )

    def _post(self, url: str, payload: bytes, headers: dict[str, str]) -> int:
        request = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310
                return int(response.status)
        except HTTPError as error:
            return int(error.code)
        except URLError as error:
            raise RuntimeError(f"Webhook connection failed: {error.reason}") from error
