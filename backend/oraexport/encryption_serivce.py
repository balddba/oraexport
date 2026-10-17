"""Credential encryption and local password authentication."""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet

from .config import get_settings


class EncryptionSerivce:
    """Encrypt credentials, verify passwords, and hash browser session tokens."""

    _hasher = PasswordHasher()

    @staticmethod
    def encrypt(value: str) -> str:
        """Encrypt a secret with the external deployment key.

        Uses Fernet symmetric authenticated cryptography (AES-128-CBC + HMAC-SHA256)
        with the key provided via application settings.

        Args:
            value (str): Plaintext secret string to encrypt.

        Returns:
            str: Fernet-encrypted ASCII ciphertext string.
        """
        # Retrieve the external encryption secret key without storing it in metadata.
        key = get_settings().encryption_key.get_secret_value()
        cipher = Fernet(key=key)
        # Encrypt the UTF-8 encoded secret into an authenticated token.
        encrypted_bytes = cipher.encrypt(data=value.encode())
        return encrypted_bytes.decode()

    @staticmethod
    def decrypt(value: str) -> str:
        """Decrypt a secret only at a runtime connection boundary.

        Decrypts ciphertext previously produced by encrypt using the configured key.

        Args:
            value (str): Fernet-encrypted ASCII ciphertext string to decrypt.

        Returns:
            str: Decrypted plaintext secret string.

        Raises:
            InvalidToken: If decryption fails due to an invalid key or corrupted payload.
        """
        # Obtain the external decryption key at the connection boundary.
        key = get_settings().encryption_key.get_secret_value()
        cipher = Fernet(key=key)
        # Decrypt and verify HMAC integrity before decoding plaintext.
        decrypted_bytes = cipher.decrypt(token=value.encode())
        return decrypted_bytes.decode()

    @staticmethod
    def hash_password(value: str) -> str:
        """Hash an application password with Argon2id.

        Args:
            value (str): Plaintext password to hash.

        Returns:
            str: Argon2id password hash string.
        """
        # Hash password with Argon2id algorithm using secure salt and cost parameters.
        return EncryptionSerivce._hasher.hash(password=value)

    @staticmethod
    def verify_password(value: str, digest: str) -> bool:
        """Verify a password without propagating invalid-password exceptions.

        Args:
            value (str): Plaintext candidate password to verify.
            digest (str): Stored Argon2id password hash digest.

        Returns:
            bool: True if the candidate matches the stored digest; False otherwise.
        """
        try:
            # Verify candidate password against Argon2id hash; handles timing resistance.
            return EncryptionSerivce._hasher.verify(hash=digest, password=value)
        except VerificationError:
            # Catch and suppress verification failures to avoid leaking error details.
            return False

    @staticmethod
    def new_session_token() -> tuple[str, str]:
        """Return an opaque token and its persisted hash.

        Generates a cryptographically secure random token for the browser cookie
        along with its SHA-256 digest for database lookup.

        Returns:
            tuple[str, str]: Pair of (opaque_cookie_token, persisted_sha256_hash).
        """
        # Generate 256 bits of URL-safe cryptographically secure randomness.
        token = secrets.token_urlsafe(nbytes=32)
        # Hash the token so raw session tokens are never stored in the database.
        digest = EncryptionSerivce.token_hash(token=token)
        return token, digest

    @staticmethod
    def token_hash(token: str) -> str:
        """Hash a session token for storage.

        Args:
            token (str): Opaque URL-safe session token.

        Returns:
            str: SHA-256 hexadecimal digest string of the token.
        """
        # Produce a deterministic SHA-256 digest of the token bytes.
        return hashlib.sha256(data=token.encode()).hexdigest()
