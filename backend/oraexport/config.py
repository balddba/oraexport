"""Validated deployment settings."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """External deployment configuration validated at process startup.

    All sensitive settings such as database credentials and encryption keys are
    loaded from environment variables or a local .env file and masked via SecretStr.
    Unknown settings are strictly rejected via extra="forbid".

    Attributes:
        metadata_url (SecretStr): Oracle database connection URL for metadata persistence.
        encryption_key (SecretStr): Base64-encoded Fernet 32-byte key for credential encryption.
        cookie_secure (bool): Whether authentication session cookies require HTTPS.
        allowed_origin (str): Allowed CORS origin for web client access.
        session_hours (int): Connection token expiration lifetime in hours (1-168).
        global_concurrency (int): Maximum concurrent export runs across all database targets.
        target_concurrency (int): Maximum concurrent export runs targeting any single database.
        poll_seconds (int): Polling frequency interval for background worker daemon loops.
        metadata_pool_max (int): Maximum metadata sessions per API or worker process.
        metadata_pool_wait_ms (int): Maximum wait for an available pooled session.
        metadata_call_timeout_ms (int): Timeout for each metadata database round trip.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="forbid", hide_input_in_errors=True)
    metadata_url: SecretStr
    encryption_key: SecretStr
    cookie_secure: bool = True
    allowed_origin: str = "https://localhost"
    session_hours: int = Field(default=12, ge=1, le=168)
    global_concurrency: int = Field(default=2, ge=1, le=32)
    target_concurrency: int = Field(default=1, ge=1, le=16)
    poll_seconds: int = Field(default=5, ge=1, le=60)
    metadata_pool_max: int = Field(default=10, ge=1, le=100)
    metadata_pool_wait_ms: int = Field(default=5000, ge=1, le=60000)
    metadata_call_timeout_ms: int = Field(default=20000, ge=1, le=120000)

    @field_validator("metadata_url")
    @classmethod
    def valid_metadata_url(cls, value: SecretStr) -> SecretStr:
        """Reject non-Oracle metadata connections before startup.

        Validates the metadata URL format and verifies that an Oracle backend is specified
        by parsing connection parameters with OracleUtilities.

        Args:
            value (SecretStr): Candidate metadata database connection URL.

        Returns:
            SecretStr: The validated metadata database connection URL.

        Raises:
            ValueError: If the metadata URL format or driver specification is invalid.
        """
        # Import lazily to break circular imports between settings and Oracle utilities.
        from oraexport.oracle_utilities import OracleUtilities

        # Ensure the connection parameters can be cleanly parsed before proceeding.
        raw_url = value.get_secret_value()
        OracleUtilities.connection_parameters(url=raw_url)
        return value

    @field_validator("encryption_key")
    @classmethod
    def valid_encryption_key(cls, value: SecretStr) -> SecretStr:
        """Fail fast if the externally supplied encryption key is invalid.

        Ensures the secret encryption key is a valid 32-byte url-safe base64-encoded
        Fernet key suitable for encrypting and decrypting database passwords.

        Args:
            value (SecretStr): Candidate symmetric encryption key secret.

        Returns:
            SecretStr: The validated symmetric encryption key secret.

        Raises:
            ValueError: If the encryption key cannot be parsed by Fernet.
        """
        # Cryptography Fernet validates that key is a url-safe base64-encoded 32-byte string.
        from cryptography.fernet import Fernet

        raw_key = value.get_secret_value()
        Fernet(key=raw_key)
        return value


@lru_cache
def get_settings() -> Settings:
    """Load and validate process settings once.

    Uses lru_cache to ensure configuration is parsed and validated exactly once per
    process lifetime, avoiding redundant environment reads and disk access.

    Returns:
        Settings: The cached singleton application settings instance.
    """
    # Instantiate Settings, triggering Pydantic model validation on environment variables.
    settings_instance = Settings()  # ty: ignore[missing-argument] -- BaseSettings loads required fields from the environment.
    return settings_instance
