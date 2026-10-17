"""AuditEventResponse Pydantic model."""

from .response_model import ResponseModel


class AuditEventResponse(ResponseModel):
    """Administrative audit log event entry.

    Attributes:
        id (str): Unique identifier for the audit event.
        user_id (str): Identifier of the user who performed the administrative action.
        action (str): Administrative action description or operation name.
        resource_id (str): Target resource identifier affected by the action.
        created_at (str): ISO formatted UTC timestamp when the event was recorded.
    """

    id: str
    user_id: str
    action: str
    resource_id: str
    created_at: str
