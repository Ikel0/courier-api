"""SQLite persistence with small, explicit repository methods.

SQLite keeps the demo runnable without infrastructure. The SQL boundaries are intentionally
isolated so a production implementation can swap this class for Postgres without changing the
HTTP contracts.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import secrets
import sqlite3
from threading import RLock
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(10).replace('-', '').replace('_', '')}"


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shipments (
                    id TEXT PRIMARY KEY,
                    reference TEXT NOT NULL,
                    recipient_json TEXT NOT NULL,
                    service_level TEXT NOT NULL,
                    parcels_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shipments_created
                    ON shipments(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_shipments_status_created
                    ON shipments(status, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS idempotency_records (
                    actor TEXT NOT NULL,
                    route TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_status INTEGER,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(actor, route, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS shipment_events (
                    id TEXT PRIMARY KEY,
                    shipment_id TEXT NOT NULL REFERENCES shipments(id),
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_shipment_created
                    ON shipment_events(shipment_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS api_deliveries (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    event TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_status INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_deliveries_created
                    ON api_deliveries(created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS webhook_endpoints (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id TEXT PRIMARY KEY,
                    endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id),
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    response_status INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_deliveries_endpoint_created
                    ON webhook_deliveries(endpoint_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    actor TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_events_created
                    ON audit_events(id DESC);
                """
            )

    @staticmethod
    def _shipment_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "reference": row["reference"],
            "recipient": json.loads(row["recipient_json"]),
            "service_level": row["service_level"],
            "parcels": json.loads(row["parcels_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_shipment(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        shipment = {
            "id": new_id("shp"),
            "reference": payload["reference"],
            "recipient": payload["recipient"],
            "service_level": payload["service_level"],
            "parcels": payload["parcels"],
            "metadata": payload["metadata"],
            "status": "created",
            "created_at": now,
            "updated_at": now,
        }
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO shipments(
                    id, reference, recipient_json, service_level, parcels_json, metadata_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shipment["id"],
                    shipment["reference"],
                    json.dumps(shipment["recipient"]),
                    shipment["service_level"],
                    json.dumps(shipment["parcels"]),
                    json.dumps(shipment["metadata"]),
                    shipment["status"],
                    shipment["created_at"],
                    shipment["updated_at"],
                ),
            )
        return shipment

    def get_shipment(self, shipment_id: str) -> dict[str, Any] | None:
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
        return self._shipment_from_row(row) if row else None

    def list_shipments(
        self,
        *,
        limit: int,
        cursor: tuple[str, str] | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        predicates: list[str] = []
        params: list[Any] = []
        if status:
            predicates.append("status = ?")
            params.append(status)
        if cursor:
            predicates.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor[0], cursor[0], cursor[1]])
        where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
        query = (
            "SELECT * FROM shipments "
            f"{where} ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        with self.read_connection() as connection:
            rows = connection.execute(query, [*params, limit]).fetchall()
        return [self._shipment_from_row(row) for row in rows]

    def update_shipment_status(self, shipment_id: str, next_status: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE shipments SET status = ?, updated_at = ? WHERE id = ?",
                (next_status, now, shipment_id),
            ).rowcount
            if not updated:
                return None
            row = connection.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
        return self._shipment_from_row(row)

    def create_event(
        self, *, shipment_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = {
            "id": new_id("evt"),
            "shipment_id": shipment_id,
            "type": event_type,
            "payload": payload,
            "created_at": utc_now(),
        }
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO shipment_events(id, shipment_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    event["id"],
                    event["shipment_id"],
                    event["type"],
                    json.dumps(event["payload"]),
                    event["created_at"],
                ),
            )
        return event

    def create_api_delivery(
        self, *, event: str, destination: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        now = utc_now()
        delivery = {
            "delivery_id": new_id("dly"),
            "trace_id": new_id("trc"),
            "event": event,
            "destination": destination,
            "payload": payload,
            "status": "accepted",
            "next": "queued for dispatch",
            "response_status": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO api_deliveries(
                    id, trace_id, event, destination, payload_json, status, response_status,
                    error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    delivery["delivery_id"],
                    delivery["trace_id"],
                    delivery["event"],
                    delivery["destination"],
                    json.dumps(delivery["payload"]),
                    delivery["status"],
                    now,
                    now,
                ),
            )
        return delivery

    @staticmethod
    def _api_delivery_from_row(row: sqlite3.Row) -> dict[str, Any]:
        status_value = row["status"]
        next_step = {
            "accepted": "queued for dispatch",
            "queued": "delivery worker will attempt the destination",
            "delivered": "receiver accepted the event",
            "failed": "inspect the trace and retry after correcting the destination",
        }.get(status_value, "inspect trace")
        return {
            "delivery_id": row["id"],
            "trace_id": row["trace_id"],
            "event": row["event"],
            "destination": row["destination"],
            "payload": json.loads(row["payload_json"]),
            "status": status_value,
            "next": next_step,
            "response_status": row["response_status"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_api_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM api_deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return self._api_delivery_from_row(row) if row else None

    def list_api_deliveries(
        self, *, limit: int, cursor: tuple[str, str] | None
    ) -> list[dict[str, Any]]:
        predicates: list[str] = []
        params: list[Any] = []
        if cursor:
            predicates.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor[0], cursor[0], cursor[1]])
        where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
        with self.read_connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM api_deliveries {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._api_delivery_from_row(row) for row in rows]

    def update_api_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        response_status: int | None,
        error: str | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE api_deliveries
                SET status = ?, response_status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, response_status, error, utc_now(), delivery_id),
            )

    def claim_idempotency(
        self, *, actor: str, route: str, key: str, request_hash: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically reserve an idempotency key, replay it, or surface a conflict."""
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT request_hash, state, response_status, response_json
                FROM idempotency_records WHERE actor = ? AND route = ? AND idempotency_key = ?
                """,
                (actor, route, key),
            ).fetchone()
            if not row:
                connection.execute(
                    """
                    INSERT INTO idempotency_records(
                        actor, route, idempotency_key, request_hash, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'processing', ?, ?)
                    """,
                    (actor, route, key, request_hash, now, now),
                )
                return "claimed", None
            if row["request_hash"] != request_hash:
                return "conflict", None
            if row["state"] == "completed":
                return "replay", {
                    "status": row["response_status"],
                    "body": json.loads(row["response_json"]),
                }
            return "processing", None

    def complete_idempotency(
        self, *, actor: str, route: str, key: str, status: int, body: dict[str, Any]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE idempotency_records
                SET state = 'completed', response_status = ?, response_json = ?, updated_at = ?
                WHERE actor = ? AND route = ? AND idempotency_key = ?
                """,
                (status, json.dumps(body), utc_now(), actor, route, key),
            )

    def abandon_idempotency(self, *, actor: str, route: str, key: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE actor = ? AND route = ? AND idempotency_key = ? AND state = 'processing'",
                (actor, route, key),
            )

    def create_webhook_endpoint(
        self, *, url: str, events: list[str], secret: str
    ) -> dict[str, Any]:
        now = utc_now()
        endpoint = {
            "id": new_id("wh"),
            "url": url,
            "events": events,
            "secret": secret,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO webhook_endpoints(id, url, events_json, secret, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (endpoint["id"], endpoint["url"], json.dumps(events), endpoint["secret"], now, now),
            )
        return endpoint

    def list_webhook_endpoints(self) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM webhook_endpoints ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "url": row["url"],
                "events": json.loads(row["events_json"]),
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "secret": row["secret"],
            }
            for row in rows
        ]

    def get_webhook_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM webhook_endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "url": row["url"],
            "events": json.loads(row["events_json"]),
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "secret": row["secret"],
        }

    def deactivate_webhook_endpoint(self, endpoint_id: str) -> bool:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE webhook_endpoints SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
                (utc_now(), endpoint_id),
            ).rowcount
        return bool(changed)

    def endpoints_for_event(self, event_type: str) -> list[dict[str, Any]]:
        return [
            endpoint
            for endpoint in self.list_webhook_endpoints()
            if endpoint["active"] and event_type in endpoint["events"]
        ]

    def create_delivery(
        self,
        *,
        endpoint_id: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        attempt: int,
    ) -> dict[str, Any]:
        delivery = {
            "id": new_id("dlv"),
            "endpoint_id": endpoint_id,
            "event_id": event_id,
            "event_type": event_type,
            "payload": payload,
            "attempt": attempt,
            "status": "pending",
            "response_status": None,
            "error": None,
            "created_at": utc_now(),
            "delivered_at": None,
        }
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                    id, endpoint_id, event_id, event_type, payload_json, attempt, status,
                    response_status, error, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    delivery["id"],
                    delivery["endpoint_id"],
                    delivery["event_id"],
                    delivery["event_type"],
                    json.dumps(delivery["payload"]),
                    delivery["attempt"],
                    delivery["status"],
                    delivery["created_at"],
                ),
            )
        return delivery

    def complete_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        response_status: int | None,
        error: str | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = ?, response_status = ?, error = ?, delivered_at = ?
                WHERE id = ?
                """,
                (status, response_status, error, utc_now(), delivery_id),
            )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "endpoint_id": row["endpoint_id"],
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "attempt": row["attempt"],
            "status": row["status"],
            "response_status": row["response_status"],
            "error": row["error"],
            "created_at": row["created_at"],
            "delivered_at": row["delivered_at"],
        }

    def list_deliveries(self, endpoint_id: str, *, limit: int) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM webhook_deliveries WHERE endpoint_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (endpoint_id, limit),
            ).fetchall()
        return [self._delivery_from_row(row) for row in rows]

    def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return self._delivery_from_row(row) if row else None

    def record_audit(
        self,
        *,
        request_id: str,
        actor: str | None,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(request_id, actor, method, path, status_code, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, actor, method, path, status_code, duration_ms, utc_now()),
            )

    def list_audit_events(self, *, limit: int) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def ping(self) -> bool:
        try:
            with self.read_connection() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
