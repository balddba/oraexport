"""Lease metadata model and Oracle table mapping."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class Lease(BaseModel):
    """Exclusive scheduler/worker lease with expiration.

    Maps to the worker_leases table. Coordinates single-instance worker operations
    such as cron scheduling, queue claims, and retention file cleanup.

    Attributes:
        name (str): Identifier or role name of the lease.
        owner (str): Worker identity currently holding the lease.
        expires_at (datetime): UTC timestamp when the lease expires.
        last_cleanup_at (datetime | None): Timestamp of previous successful cleanup run.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Oracle table name mapping.
    __tablename__: ClassVar[str] = "worker_leases"
    name: str
    owner: str
    expires_at: datetime
    last_cleanup_at: datetime | None = None
    # Oracle storage descriptors.
    __pk__: ClassVar[str] = "name"
    __lobs__: ClassVar[tuple[str, ...]] = ()
