"""Shared validation helpers for application models."""

import re


def identifier(value: str) -> str:
    """Validate a conventional Oracle identifier without SQL metacharacters.

    Ensures the identifier conforms to Oracle naming rules (letters, digits, _, $, #)
    and does not exceed 128 characters, protecting against SQL injection when used
    in dynamic DDL and Data Pump API parameterization.

    Args:
        value (str): Candidate identifier string to validate.

    Returns:
        str: Uppercased Oracle identifier matching naming rules.

    Raises:
        ValueError: If value is not a valid unquoted Oracle identifier.
    """
    uppercased = value.upper()
    is_valid = bool(re.fullmatch(pattern=r"[A-Z][A-Z0-9_$#]{0,127}", string=uppercased))
    if not is_valid:
        raise ValueError("Use an unquoted Oracle identifier (letters, digits, _, $, #)")
    return uppercased


def connection_dsn(value: str) -> str:
    """Keep credentials out of public DSN fields and require separate encrypted inputs.

    Validates that database connection strings do not embed plaintext passwords or
    credentials, enforcing strict credential isolation into encrypted vault storage.

    Args:
        value (str): Candidate Oracle database connection DSN string.

    Returns:
        str: Validated DSN string without embedded credentials.

    Raises:
        ValueError: If DSN embeds credentials or secret keywords.
    """
    # Import lazily to avoid circular dependencies and extra import overhead.
    from urllib.parse import unquote

    decoded = unquote(string=value)
    has_at_sign = "@" in decoded
    has_password = bool(re.search(pattern=r"(?i)(password|passwd|pwd|token)\s*=", string=decoded))
    if has_at_sign or has_password:
        raise ValueError("DSNs cannot embed credentials; use the separate username/password fields")
    return value
