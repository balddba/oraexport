"""Notifications HTTP endpoints.

This module provides FastAPI routes for managing alerting channel configurations
(SMTP email and webhook endpoints), staging and dispatching test events through the
durable notification outbox, and inspecting paginated delivery logs and retry states.
"""

from fastapi import APIRouter, Depends, Query

from oraexport.metadata_service import MetadataService
from oraexport.models.delivery_item_response import DeliveryItemResponse
from oraexport.models.delivery_list_response import DeliveryListResponse
from oraexport.models.notification_input import NotificationInput
from oraexport.models.notification_settings_response import NotificationSettingsResponse
from oraexport.models.status_response import StatusResponse

from ...bootstrap import notification_service
from ...notifications import NotificationService
from ..dependencies import Admin, Db, fields

router = APIRouter()


@router.get(path="/api/v1/notifications/settings")
def notification_settings(
    db: Db,
    user: Admin,
    service: NotificationService = Depends(dependency=notification_service),
) -> NotificationSettingsResponse:
    """Read public notification configuration and secret-presence indicators.

    Retrieves notification channel parameters while masking encrypted secret values
    (e.g., SMTP passwords, webhook signing secrets) behind boolean presence flags.

    Args:
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (NotificationService): Composed notification application service.

    Returns:
        NotificationSettingsResponse: Sanitized notification settings with presence flags.
    """
    # Query configuration without decrypting secrets into the HTTP response
    result = service.notification_settings(db=db, user=user)
    return result


@router.put(path="/api/v1/notifications/settings")
def save_notifications(
    body: NotificationInput,
    db: Db,
    user: Admin,
    service: NotificationService = Depends(dependency=notification_service),
) -> StatusResponse:
    """Save notification endpoints while keeping omitted secrets intact.

    Updates SMTP and webhook endpoint settings. If secret fields are omitted in the
    input payload, existing encrypted secrets are preserved in the metadata database.

    Args:
        body (NotificationInput): Validated notification configuration payload.
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (NotificationService): Composed notification application service.

    Returns:
        StatusResponse: Confirmation of updated notification settings.
    """
    # Persist updated endpoints and encrypt any newly provided credentials
    result = service.save_notifications(body=body, db=db, user=user)
    return result


@router.post(path="/api/v1/notifications/test")
def test_notifications(
    db: Db,
    user: Admin,
    service: NotificationService = Depends(dependency=notification_service),  # noqa: PT028
) -> StatusResponse:
    """Queue a test event through the normal durable delivery path.

    Enqueues a synthetic 'test' notification event in the durable outbox table,
    allowing administrators to verify channel connectivity through the worker daemon.

    Args:
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        service (NotificationService): Composed notification application service.

    Returns:
        StatusResponse: Enqueued test event confirmation.
    """
    # Insert transactional test event record into the metadata outbox table
    result = service.test_notifications(db=db, user=user)
    return result


@router.get(path="/api/v1/notifications/deliveries")
def deliveries(
    db: Db,
    user: Admin,
    page: int = Query(default=1, ge=1),
) -> DeliveryListResponse:
    """Inspect notification delivery status and bounded retry failures.

    Returns a paginated list of notification outbox records including channel,
    attempt counts, scheduled retry timestamps, and recorded delivery error text.

    Args:
        db (Db): Active metadata database session dependency.
        user (Admin): Authenticated administrative caller enforcing RBAC.
        page (int): 1-indexed page number for pagination.

    Returns:
        DeliveryListResponse: Paginated delivery records and total delivery count.
    """
    # Decompose delivery query tuple into single-line variable assignments
    delivery_page = MetadataService(db=db).deliveries(page=page)
    entries = delivery_page[0]
    total = delivery_page[1]
    # Serialize delivery records excluding raw sensitive payload blobs
    field_list = "id channel state attempts next_attempt_at error"
    items = [DeliveryItemResponse.model_validate(obj=fields(item=item, names=field_list)) for item in entries]
    result_data = DeliveryListResponse(items=items, total=total)
    return result_data
