"""Sanitized Oracle transport failures."""

import re


def safe_error(exc: Exception) -> str:
    """Extract error codes without exposing connection strings, SQL, or credentials.

    Scans the string representation of an exception for standard Oracle error codes
    (ORA-XXXXX from database server, DPY-XXXX from python-oracledb, DPI-XXXX from ODPI-C)
    and formats unique codes into a comma-separated string.

    Args:
        exc (Exception): Caught exception from Oracle driver or network operation.

    Returns:
        str: Comma-separated list of ORA, DPY, or DPI error codes, or exception type name.
    """
    # Match ORA, DPY, or DPI error numbers using non-capturing prefix group.
    codes = re.findall(pattern=r"(?:ORA|DPY|DPI)-\d+", string=str(exc))
    # Deduplicate codes while preserving order, falling back to class name if no codes match.
    unique_codes = list(dict.fromkeys(codes))
    return ", ".join(unique_codes) or type(exc).__name__
