"""Sanitized Oracle operation failures."""


class OracleUnavailable(RuntimeError):  # noqa: N818
    """A remote operation failed; expose a sanitized Oracle error code only.

    Domain and API exception representing Oracle database unavailability or execution failure
    without exposing confidential connect strings, credentials, or internal SQL fragments.
    """
