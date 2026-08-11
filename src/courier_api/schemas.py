"""Public request and response contracts for the generated OpenAPI document."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ShipmentStatus(StrEnum):
    CREATED = "created"
    LABELLED = "labelled"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"


class Recipient(BaseModel):
    name: str = Field(min_length=2, max_length=120, examples=["Maya N'Diaye"])
    email: str | None = Field(default=None, max_length=254, examples=["maya@example.com"])
    phone: str | None = Field(default=None, max_length=32, examples=["+33601020304"])
    address_line1: str = Field(min_length=3, max_length=160)
    postal_code: str = Field(min_length=2, max_length=20)
    city: str = Field(min_length=2, max_length=80)
    country_code: str = Field(min_length=2, max_length=2, examples=["FR"])

    @field_validator("country_code")
    @classmethod
    def country_code_is_uppercase(cls, value: str) -> str:
        return value.upper()


class Parcel(BaseModel):
    weight_grams: int = Field(ge=1, le=30_000)
    length_cm: float = Field(gt=0, le=200)
    width_cm: float = Field(gt=0, le=200)
    height_cm: float = Field(gt=0, le=200)


class ShipmentCreate(BaseModel):
    reference: str = Field(min_length=3, max_length=80, examples=["ORDER-2026-042"])
    recipient: Recipient
    service_level: Literal["standard", "express"] = "standard"
    parcels: list[Parcel] = Field(min_length=1, max_length=25)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)


class ShipmentStatusUpdate(BaseModel):
    status: ShipmentStatus
    note: str | None = Field(default=None, max_length=300)


class ShipmentEventCreate(BaseModel):
    type: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_.-]+$",
        examples=["shipment.delivery_window_updated"],
    )
    payload: dict[str, Any] = Field(default_factory=dict)


class ShipmentResponse(BaseModel):
    id: str
    reference: str
    recipient: Recipient
    service_level: str
    parcels: list[Parcel]
    metadata: dict[str, str]
    status: ShipmentStatus
    created_at: datetime
    updated_at: datetime


class ShipmentPage(BaseModel):
    data: list[ShipmentResponse]
    next_cursor: str | None = None


class WebhookEndpointCreate(BaseModel):
    url: str = Field(examples=["https://example.com/webhooks/courier"])
    events: list[str] = Field(
        min_length=1,
        max_length=20,
        examples=[["shipment.created", "shipment.status_changed"]],
    )
    secret: str | None = Field(
        default=None,
        min_length=24,
        max_length=256,
        description="Optional signing secret. If omitted, one is generated and returned once.",
    )


class WebhookEndpointResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    active: bool
    created_at: datetime
    secret: str | None = Field(
        default=None,
        description="Shown only when an endpoint is created. Store it safely.",
    )


class WebhookDeliveryResponse(BaseModel):
    id: str
    endpoint_id: str
    event_id: str
    event_type: str
    attempt: int
    status: str
    response_status: int | None
    error: str | None
    created_at: datetime
    delivered_at: datetime | None


class AuditEventResponse(BaseModel):
    id: int
    request_id: str
    actor: str | None
    method: str
    path: str
    status_code: int
    duration_ms: float
    created_at: datetime


class AuditEventPage(BaseModel):
    data: list[AuditEventResponse]


class DemoKeyRequest(BaseModel):
    label: str = Field(default="playground", min_length=2, max_length=60)
    ttl_minutes: int = Field(default=30, ge=1, le=60)


class DemoKeyResponse(BaseModel):
    key: str
    expires_in_seconds: int
    scope: list[str]


class DeliveryCreate(BaseModel):
    """A compact direct-webhook surface used by the interactive playground."""

    event: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_.-]+$",
        examples=["invoice.paid"],
    )
    destination: str = Field(examples=["https://example.com/webhooks/billing"])
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description="Convenience echo for the playground. The Idempotency-Key header is authoritative.",
    )


class DeliveryResponse(BaseModel):
    delivery_id: str
    trace_id: str
    event: str
    destination: str
    status: str
    next: str
    created_at: datetime


class DeliveryPage(BaseModel):
    items: list[DeliveryResponse]
    next_cursor: str | None = None
