"""Courier API application factory."""

from __future__ import annotations

import logging
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .config import Settings
from .db import Database
from .delivery import WebhookDispatcher
from .schemas import (
    AuditEventPage,
    DeliveryCreate,
    DeliveryPage,
    DeliveryResponse,
    DemoKeyRequest,
    DemoKeyResponse,
    ShipmentCreate,
    ShipmentEventCreate,
    ShipmentPage,
    ShipmentResponse,
    ShipmentStatus,
    ShipmentStatusUpdate,
    WebhookDeliveryResponse,
    WebhookEndpointCreate,
    WebhookEndpointResponse,
)
from .security import (
    APIKeyAuthenticator,
    APIPrincipal,
    SlidingWindowRateLimiter,
    decode_cursor,
    encode_cursor,
    request_fingerprint,
    require_api_key,
    validate_idempotency_key,
    validate_webhook_url,
)
from .seed import seed_demo_data


logger = logging.getLogger("courier_api")


VALID_TRANSITIONS: dict[str, set[str]] = {
    "created": {"labelled", "cancelled"},
    "labelled": {"in_transit", "cancelled"},
    "in_transit": {"out_for_delivery", "exception"},
    "out_for_delivery": {"delivered", "exception"},
    "exception": {"in_transit", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}


class Metrics:
    def __init__(self) -> None:
        self.total_requests = 0
        self.server_errors = 0
        self.webhook_attempts = 0

    def render(self) -> str:
        return "\n".join(
            [
                "# HELP courier_http_requests_total Number of requests handled by Courier API.",
                "# TYPE courier_http_requests_total counter",
                f"courier_http_requests_total {self.total_requests}",
                "# HELP courier_http_server_errors_total Number of requests ending in a 5xx response.",
                "# TYPE courier_http_server_errors_total counter",
                f"courier_http_server_errors_total {self.server_errors}",
            ]
        ) + "\n"


def _safe_endpoint(endpoint: dict[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    result = {
        "id": endpoint["id"],
        "url": endpoint["url"],
        "events": endpoint["events"],
        "active": endpoint["active"],
        "created_at": endpoint["created_at"],
    }
    if include_secret:
        result["secret"] = endpoint["secret"]
    return result


def _idempotency_decision(
    *,
    database: Database,
    actor: str,
    route: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> JSONResponse | None:
    decision, record = database.claim_idempotency(
        actor=actor,
        route=route,
        key=idempotency_key,
        request_hash=request_fingerprint(payload),
    )
    if decision == "replay":
        assert record is not None
        return JSONResponse(
            status_code=int(record["status"]),
            content=record["body"],
            headers={"Idempotency-Replayed": "true"},
        )
    if decision == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key has already been used with a different request body.",
        )
    if decision == "processing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A request using this Idempotency-Key is still being processed.",
        )
    return None


def _not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{resource} was not found.")


def _masked_destination(value: str) -> str:
    """Keep the live dashboard useful without exposing a full integration URL."""
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}/…" if parsed.scheme and parsed.netloc else "masked"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    if settings.seed_demo_data:
        seed_demo_data(database)

    app = FastAPI(
        title="Courier API",
        version="0.1.0",
        summary="Une API de livraison pensée comme une surface de production.",
        description=(
            "Courier API est un projet de démonstration : authentification par clé API, "
            "idempotence, curseurs, webhooks signés et journal d'audit. "
            "La documentation interactive est disponible ici et le contrat OpenAPI via /openapi.json."
        ),
        contact={"name": "Ikel Ouedraogo", "email": "IkelITech@outlook.com"},
        license_info={"name": "MIT"},
    )
    app.state.database = database
    app.state.settings = settings
    app.state.authenticator = APIKeyAuthenticator(settings)
    app.state.rate_limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute)
    app.state.dispatcher = WebhookDispatcher(database, settings.webhook_timeout_seconds)
    app.state.metrics = Metrics()
    web_directory = Path(__file__).resolve().parents[2] / "web"

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        request.state.request_id = request_id
        started = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            status_code = response.status_code if response else 500
            app.state.metrics.total_requests += 1
            if status_code >= 500:
                app.state.metrics.server_errors += 1
            try:
                database.record_audit(
                    request_id=request_id,
                    actor=getattr(request.state, "actor", None),
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    duration_ms=elapsed_ms,
                )
            except Exception:
                logger.exception("Unable to persist audit event", extra={"request_id": request_id})
            if response is not None:
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Cache-Control"] = "no-store"

    @app.get("/", include_in_schema=False)
    async def project_home() -> Response:
        index = web_directory / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {
                "service": "Courier API",
                "documentation": "/docs",
                "openapi": "/openapi.json",
                "liveness": "/healthz",
                "readiness": "/readyz",
            }
        )

    @app.get("/style.css", include_in_schema=False)
    async def project_style() -> Response:
        return FileResponse(web_directory / "style.css")

    @app.get("/app.js", include_in_schema=False)
    async def project_script() -> Response:
        return FileResponse(web_directory / "app.js")

    @app.get("/api", tags=["Service"])
    async def service_index() -> dict[str, str]:
        return {
            "service": "Courier API",
            "documentation": "/docs",
            "openapi": "/openapi.json",
            "liveness": "/healthz",
            "readiness": "/readyz",
        }

    @app.get("/healthz", tags=["Service"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health", tags=["Service"])
    async def playground_health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "courier-api",
            "version": "0.1.0",
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @app.get("/readyz", tags=["Service"])
    async def readiness() -> Response:
        if not database.ping():
            return JSONResponse(status_code=503, content={"status": "not_ready", "database": "down"})
        return JSONResponse(content={"status": "ready", "database": "ok"})

    @app.get("/metrics", response_class=PlainTextResponse, tags=["Service"])
    async def metrics() -> str:
        return app.state.metrics.render()

    @app.post("/api/demo", response_model=DemoKeyResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
    @app.post("/v1/demo", response_model=DemoKeyResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
    @app.post(
        "/v1/keys/demo",
        response_model=DemoKeyResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Demo"],
        summary="Créer une clé éphémère pour le playground",
    )
    async def create_demo_key(body: DemoKeyRequest) -> dict[str, Any]:
        if not settings.enable_demo_keys:
            raise _not_found("Demo key endpoint")
        ttl_seconds = body.ttl_minutes * 60
        return {
            "key": app.state.authenticator.issue_demo_key(ttl_seconds=ttl_seconds),
            "expires_in_seconds": ttl_seconds,
            "scope": ["deliveries:read", "deliveries:write"],
        }

    @app.post(
        "/v1/shipments",
        response_model=ShipmentResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Shipments"],
        responses={
            400: {"description": "Missing or malformed idempotency key."},
            401: {"description": "Missing or invalid API key."},
            409: {"description": "Idempotency conflict."},
        },
    )
    async def create_shipment(
        body: ShipmentCreate,
        background_tasks: BackgroundTasks,
        principal: APIPrincipal = Depends(require_api_key),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        key = validate_idempotency_key(idempotency_key)
        payload = body.model_dump(mode="json")
        replay = _idempotency_decision(
            database=database,
            actor=principal.key_id,
            route="POST:/v1/shipments",
            idempotency_key=key,
            payload=payload,
        )
        if replay:
            return replay
        try:
            shipment = database.create_shipment(payload)
            event = database.create_event(
                shipment_id=shipment["id"],
                event_type="shipment.created",
                payload={"shipment": shipment},
            )
            database.complete_idempotency(
                actor=principal.key_id,
                route="POST:/v1/shipments",
                key=key,
                status=201,
                body=shipment,
            )
        except Exception:
            database.abandon_idempotency(actor=principal.key_id, route="POST:/v1/shipments", key=key)
            raise
        background_tasks.add_task(app.state.dispatcher.dispatch, event)
        return JSONResponse(
            status_code=201,
            content=shipment,
            headers={"Location": f"/v1/shipments/{shipment['id']}"},
        )

    @app.post(
        "/v1/deliveries",
        response_model=DeliveryResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Playground deliveries"],
        summary="Accepter une demande d'envoi webhooks avec trace durable",
    )
    async def create_delivery(
        body: DeliveryCreate,
        background_tasks: BackgroundTasks,
        principal: APIPrincipal = Depends(require_api_key),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        key = validate_idempotency_key(idempotency_key or body.idempotency_key)
        route = "POST:/v1/deliveries"
        payload = body.model_dump(mode="json")
        replay = _idempotency_decision(
            database=database,
            actor=principal.key_id,
            route=route,
            idempotency_key=key,
            payload=payload,
        )
        if replay:
            return replay
        try:
            destination = validate_webhook_url(
                body.destination, allow_insecure=settings.allow_insecure_webhooks
            )
            delivery = database.create_api_delivery(
                event=body.event, destination=destination, payload=body.payload
            )
            database.complete_idempotency(
                actor=principal.key_id,
                route=route,
                key=key,
                status=202,
                body=delivery,
            )
        except Exception:
            database.abandon_idempotency(actor=principal.key_id, route=route, key=key)
            raise
        background_tasks.add_task(app.state.dispatcher.dispatch_api_delivery, delivery["delivery_id"])
        return JSONResponse(status_code=202, content=delivery)

    @app.get(
        "/v1/deliveries",
        response_model=DeliveryPage,
        tags=["Playground deliveries"],
        summary="Lire les demandes d'envoi par curseur",
    )
    async def list_deliveries(
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None),
        _: APIPrincipal = Depends(require_api_key),
    ) -> dict[str, Any]:
        decoded_cursor = decode_cursor(cursor) if cursor else None
        rows = database.list_api_deliveries(limit=limit + 1, cursor=decoded_cursor)
        has_next = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1]["created_at"], page[-1]["delivery_id"])
            if has_next and page
            else None
        )
        return {"items": page, "next_cursor": next_cursor}

    @app.get("/api/overview", tags=["Demo"], include_in_schema=False)
    async def playground_overview() -> dict[str, Any]:
        """A deliberately redacted public overview consumed by the project landing page."""
        rows = database.list_api_deliveries(limit=12, cursor=None)
        deliveries = [
            {
                "id": item["trace_id"],
                "trace_id": item["trace_id"],
                "event": item["event"],
                "destination": _masked_destination(item["destination"]),
                "status": item["status"],
                "created_at": item["created_at"],
                "detail": item["next"],
            }
            for item in rows
        ]
        return {"deliveries": deliveries, "generated_at": time.time()}

    @app.get(
        "/v1/shipments",
        response_model=ShipmentPage,
        tags=["Shipments"],
    )
    async def list_shipments(
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None),
        shipment_status: ShipmentStatus | None = Query(default=None, alias="status"),
        _: APIPrincipal = Depends(require_api_key),
    ) -> dict[str, Any]:
        decoded_cursor = decode_cursor(cursor) if cursor else None
        rows = database.list_shipments(
            limit=limit + 1,
            cursor=decoded_cursor,
            status=shipment_status.value if shipment_status else None,
        )
        has_next = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1]["created_at"], page[-1]["id"])
            if has_next and page
            else None
        )
        return {"data": page, "next_cursor": next_cursor}

    @app.get(
        "/v1/shipments/{shipment_id}",
        response_model=ShipmentResponse,
        tags=["Shipments"],
    )
    async def get_shipment(
        shipment_id: str, _: APIPrincipal = Depends(require_api_key)
    ) -> dict[str, Any]:
        shipment = database.get_shipment(shipment_id)
        if not shipment:
            raise _not_found("Shipment")
        return shipment

    @app.patch(
        "/v1/shipments/{shipment_id}/status",
        response_model=ShipmentResponse,
        tags=["Shipments"],
    )
    async def update_shipment_status(
        shipment_id: str,
        body: ShipmentStatusUpdate,
        background_tasks: BackgroundTasks,
        principal: APIPrincipal = Depends(require_api_key),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        key = validate_idempotency_key(idempotency_key)
        payload = {"shipment_id": shipment_id, **body.model_dump(mode="json")}
        route = "PATCH:/v1/shipments/:id/status"
        replay = _idempotency_decision(
            database=database,
            actor=principal.key_id,
            route=route,
            idempotency_key=key,
            payload=payload,
        )
        if replay:
            return replay
        try:
            current = database.get_shipment(shipment_id)
            if not current:
                raise _not_found("Shipment")
            if body.status.value not in VALID_TRANSITIONS[current["status"]]:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot transition from {current['status']} to {body.status.value}.",
                )
            shipment = database.update_shipment_status(shipment_id, body.status.value)
            assert shipment is not None
            event = database.create_event(
                shipment_id=shipment_id,
                event_type="shipment.status_changed",
                payload={
                    "shipment": shipment,
                    "previous_status": current["status"],
                    "note": body.note,
                },
            )
            database.complete_idempotency(
                actor=principal.key_id, route=route, key=key, status=200, body=shipment
            )
        except Exception:
            database.abandon_idempotency(actor=principal.key_id, route=route, key=key)
            raise
        background_tasks.add_task(app.state.dispatcher.dispatch, event)
        return JSONResponse(status_code=200, content=shipment)

    @app.post(
        "/v1/shipments/{shipment_id}/events",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Shipments"],
    )
    async def create_shipment_event(
        shipment_id: str,
        body: ShipmentEventCreate,
        background_tasks: BackgroundTasks,
        principal: APIPrincipal = Depends(require_api_key),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        key = validate_idempotency_key(idempotency_key)
        route = "POST:/v1/shipments/:id/events"
        payload = {"shipment_id": shipment_id, **body.model_dump(mode="json")}
        replay = _idempotency_decision(
            database=database,
            actor=principal.key_id,
            route=route,
            idempotency_key=key,
            payload=payload,
        )
        if replay:
            return replay
        try:
            if not database.get_shipment(shipment_id):
                raise _not_found("Shipment")
            event = database.create_event(
                shipment_id=shipment_id, event_type=body.type, payload=body.payload
            )
            response = {"event_id": event["id"], "status": "accepted"}
            database.complete_idempotency(
                actor=principal.key_id, route=route, key=key, status=202, body=response
            )
        except Exception:
            database.abandon_idempotency(actor=principal.key_id, route=route, key=key)
            raise
        background_tasks.add_task(app.state.dispatcher.dispatch, event)
        return JSONResponse(status_code=202, content=response)

    @app.post(
        "/v1/webhook-endpoints",
        response_model=WebhookEndpointResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Webhooks"],
    )
    async def create_webhook_endpoint(
        body: WebhookEndpointCreate,
        principal: APIPrincipal = Depends(require_api_key),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        key = validate_idempotency_key(idempotency_key)
        payload = body.model_dump(mode="json")
        route = "POST:/v1/webhook-endpoints"
        replay = _idempotency_decision(
            database=database,
            actor=principal.key_id,
            route=route,
            idempotency_key=key,
            payload=payload,
        )
        if replay:
            return replay
        try:
            url = validate_webhook_url(
                body.url, allow_insecure=settings.allow_insecure_webhooks
            )
            secret = body.secret or secrets.token_urlsafe(32)
            endpoint = database.create_webhook_endpoint(url=url, events=body.events, secret=secret)
            response = _safe_endpoint(endpoint, include_secret=True)
            database.complete_idempotency(
                actor=principal.key_id, route=route, key=key, status=201, body=response
            )
        except Exception:
            database.abandon_idempotency(actor=principal.key_id, route=route, key=key)
            raise
        return JSONResponse(status_code=201, content=response)

    @app.get(
        "/v1/webhook-endpoints",
        response_model=list[WebhookEndpointResponse],
        tags=["Webhooks"],
    )
    async def list_webhook_endpoints(
        _: APIPrincipal = Depends(require_api_key),
    ) -> list[dict[str, Any]]:
        return [_safe_endpoint(endpoint) for endpoint in database.list_webhook_endpoints()]

    @app.delete(
        "/v1/webhook-endpoints/{endpoint_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["Webhooks"],
    )
    async def deactivate_webhook_endpoint(
        endpoint_id: str, _: APIPrincipal = Depends(require_api_key)
    ) -> Response:
        if not database.deactivate_webhook_endpoint(endpoint_id):
            raise _not_found("Active webhook endpoint")
        return Response(status_code=204)

    @app.get(
        "/v1/webhook-endpoints/{endpoint_id}/deliveries",
        response_model=list[WebhookDeliveryResponse],
        tags=["Webhooks"],
    )
    async def list_webhook_deliveries(
        endpoint_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        _: APIPrincipal = Depends(require_api_key),
    ) -> list[dict[str, Any]]:
        if not database.get_webhook_endpoint(endpoint_id):
            raise _not_found("Webhook endpoint")
        return database.list_deliveries(endpoint_id, limit=limit)

    @app.post(
        "/v1/webhook-endpoints/{endpoint_id}/deliveries/{delivery_id}/retry",
        response_model=WebhookDeliveryResponse,
        tags=["Webhooks"],
    )
    async def retry_webhook_delivery(
        endpoint_id: str,
        delivery_id: str,
        _: APIPrincipal = Depends(require_api_key),
    ) -> dict[str, Any]:
        result = await app.state.dispatcher.retry(endpoint_id, delivery_id)
        if not result:
            raise _not_found("Webhook delivery")
        return result

    @app.get(
        "/v1/audit-events",
        response_model=AuditEventPage,
        tags=["Operations"],
    )
    async def list_audit_events(
        limit: int = Query(default=50, ge=1, le=100),
        _: APIPrincipal = Depends(require_api_key),
    ) -> dict[str, Any]:
        return {"data": database.list_audit_events(limit=limit)}

    return app


app = create_app()
