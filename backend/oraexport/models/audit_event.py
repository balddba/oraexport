"""AuditEvent metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .defaults import now, uid


class AuditEvent(BaseModel):
    """Secret-free administrative action audit trail.

    Maps to the audit_events table. Stores an immutable log of administrative actions
    excluding sensitive credential ciphertexts and authentication secrets.

    Attributes:
        id (str): Unique identifier for the audit event.
        user_id (str | None): Identifier of user who performed administrative action, if known.
        action (str): Administrative action description or operation name.
        resource_id (str): Target resource identifier affected by the action.
        created_at (datetime): UTC timestamp when the audit event was recorded.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "audit_events"
    id: str = Field(default_factory=uid)
    user_id: str | None = None
    action: str
    resource_id: str
    created_at: datetime = Field(default_factory=now)
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "id"
    __lobs__: ClassVar[tuple[str, ...]] = ()
