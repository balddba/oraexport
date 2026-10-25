"""Shared Oracle driver value, DSN, and connection utilities."""

import re
from contextlib import contextmanager
from typing import Generator
from urllib.parse import parse_qs, unquote, urlsplit

import oracledb

from oraexport.encryption_serivce import EncryptionSerivce
from oraexport.exceptions.oracle_unavailable import OracleUnavailable
from oraexport.exceptions.safe_error import safe_error
from oraexport.models.credential import Credential
from oraexport.models.oracle_connection_parameters import OracleConnectionParameters
from oraexport.types import OracleLobVariable


class OracleUtilities:
    """Normalize Oracle values, parse service DSNs, and manage remote connection scopes."""

    @staticmethod
    def lob_lines(variable: OracleLobVariable) -> list[str]:
        """Convert a CLOB bind or return variable to text lines.

        Normalizes output from python-oracledb LOB variables. Depending on driver
        version, configuration, and size, python-oracledb may return either an
        oracledb.LOB locator object exposing a read() streaming method or a direct
        pre-fetched Python string. This method normalizes both representations
        into a list of individual text lines.

        Args:
            variable (OracleLobVariable): Oracle cursor variable, bind variable, or LOB locator.

        Returns:
            list[str]: Normalized sequence of text lines extracted from the LOB value.
        """
        # python-oracledb getvalue() returns LOB locator object or pre-fetched str.
        value = variable.getvalue()
        # Stream LOB locator content if read() method exists, otherwise use scalar.
        reader = getattr(value, "read", None)
        if callable(reader):
            value = reader()
        if value is None:
            return []
        if not isinstance(value, str):
            raise TypeError("Expected text from Oracle CLOB variable")
        lines = value.splitlines()
        return lines

    @staticmethod
    def parse_dsn(dsn: str, service_name: str) -> str | None:
        """Reuse a registration's listener and transport settings for a discovered service.

        Parses the base connection descriptor, substitutes the target service name,
        and returns a shortened Easy Connect string when protocol and host settings match.

        Args:
            dsn (str): Original Oracle Easy Connect string or connect descriptor.
            service_name (str): Discovered service name to substitute into connect parameters.

        Returns:
            str | None: Discovered service connection string, or None if invalid.
        """
        try:
            # Parse connection string into driver parameters to extract host, port, protocol.
            params = oracledb.ConnectParams()
            params.parse_connect_string(connect_string=dsn)
            # SIDs are legacy and not supported for pluggable database service discovery.
            if params.sid:
                return None
            params.set(service_name=service_name)
            result = params.get_connect_string()
            valid_host = isinstance(params.host, str)
            valid_port = isinstance(params.port, int)
            valid_svc = bool(re.fullmatch(pattern=r"[\w.$-]+", string=service_name))
            if valid_host and valid_port and valid_svc:
                # Format IPv6 addresses enclosed in brackets.
                host = f"[{params.host}]" if ":" in params.host else params.host
                protocol = "tcps://" if params.protocol == "tcps" else ""
                easy = f"{protocol}{host}:{params.port}/{service_name}"
                has_query = not dsn.lstrip().startswith("(") and "?" in dsn
                if has_query:
                    query_part = dsn.split("?", 1)[1]
                    easy += f"?{query_part}"
                candidate = oracledb.ConnectParams()
                candidate.parse_connect_string(connect_string=easy)
                # Only shorten when the driver's descriptor preserves every setting.
                if candidate.get_connect_string() == result:
                    result = easy
            return result if result and len(result) <= 1000 else None
        except oracledb.Error:
            return None

    @staticmethod
    @contextmanager
    def connect(dsn: str, credential: Credential) -> Generator[oracledb.Connection, None, None]:
        """Connect using decrypted credentials, with bounded calls and sanitized errors.

        Opens a short-lived thin-mode Oracle connection, configures English NLS language,
        sets network timeouts, yields the raw driver connection, and ensures deterministic cleanup.

        Args:
            dsn (str): Oracle Easy Connect string or descriptor for the target database.
            credential (Credential): Username, encrypted password, and SYSDBA mode.

        Yields:
            oracledb.Connection: Active authenticated driver connection.

        Raises:
            OracleUnavailable: If the Oracle connection fails or network call times out.
        """
        connection = None
        try:
            # Decrypt password at the connect boundary only; never store or log plaintext.
            raw_password = EncryptionSerivce.decrypt(value=credential.ciphertext)
            auth_mode = oracledb.AUTH_MODE_SYSDBA if credential.sysdba else oracledb.AUTH_MODE_DEFAULT
            # Configure bounded connection options: 10s TCP handshake timeout.
            connection = oracledb.connect(user=credential.username, password=raw_password, dsn=dsn, mode=auth_mode, tcp_connect_timeout=10)
            # Enforce a 20s call timeout on all socket queries to prevent indefinite hangs.
            connection.call_timeout = 20000
            # Set English server messages for consistent error and log parsing.
            with connection.cursor() as setup_cursor:
                setup_cursor.execute("ALTER SESSION SET NLS_LANGUAGE = 'AMERICAN'")
            yield connection
        except oracledb.Error as exc:
            # Wrap driver errors into sanitized OracleUnavailable carrying only ORA error codes.
            sanitized = safe_error(exc=exc)
            raise OracleUnavailable(sanitized) from None
        finally:
            # Deterministically close thin connection to avoid server-side session leaks.
            if connection:
                connection.close()

    @staticmethod
    def connection_parameters(url: str) -> OracleConnectionParameters:
        """Parse an Oracle connection URL without exposing credentials in errors.

        Args:
            url (str): Oracle URL with percent-encoded user and password.

        Returns:
            OracleConnectionParameters: Typed Oracle credentials, DSN, and timeout.

        Raises:
            ValueError: If the URL is not a supported Oracle service connection.
        """
        try:
            parsed = urlsplit(url=url)
            if parsed.scheme not in {"oracle", "oracle+oracledb"}:
                raise ValueError
            query = parse_qs(qs=parsed.query, strict_parsing=True)
            if set(query) - {"service_name"}:
                raise ValueError
            fallback_service = [parsed.path.strip("/")]
            service = query.get("service_name", fallback_service)[0]
            if not parsed.hostname or not parsed.username or parsed.password is None or not service:
                raise ValueError
            if len(query.get("service_name", [])) > 1 or parsed.fragment:
                raise ValueError
            resolved_port = parsed.port or 1521
            computed_dsn = oracledb.makedsn(host=parsed.hostname, port=resolved_port, service_name=service)
            return OracleConnectionParameters(
                user=unquote(string=parsed.username),
                password=unquote(string=parsed.password),
                dsn=computed_dsn,
                tcp_connect_timeout=10,
            )
        except (ValueError, TypeError):
            raise ValueError("METADATA_URL must be an Oracle URL with a user, password, and service") from None
