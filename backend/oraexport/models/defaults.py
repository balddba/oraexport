"""Application-side identifiers and UTC timestamp defaults for metadata."""

import uuid
from datetime import datetime, timezone


def uid() -> str:
    """Generate a unique hex string primary key.

    Returns:
        str: 32-character lowercase hexadecimal UUID string.
    """
    # Produce a 128-bit random UUID formatted as a 32-character hex string.
    return uuid.uuid4().hex


def now() -> datetime:
    """Return naive UTC for Oracle timestamp handling.

    Oracle standard TIMESTAMP columns store date and time components without timezone
    offsets; this helper produces naive UTC datetimes to ensure consistent timezone-less storage.

    Returns:
        datetime: Current datetime in UTC with tzinfo stripped.
    """
    # Query current UTC time and strip tzinfo object for Oracle TIMESTAMP binding.
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)
