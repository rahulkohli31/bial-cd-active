"""Symmetric at-rest encryption for app-role passwords (Fernet).

One role serves both the sandbox (re-injected on every container birth) and the deployed
app (an ACA secret set once at go-live), so the password cannot be reset on demand without
cutting the live deployment off — it has to survive in the registry, encrypted. Fernet
(AES-128-CBC + HMAC-SHA256, authenticated) is the whole primitive: `cryptography` is a
declared dependency and there is no key-management story here beyond "one key from config".

Key rotation is a documented MANUAL story this phase (re-encrypt the registry column under
a new key), not code — hence plain `Fernet`, not `MultiFernet`.

The plaintext password is never logged, never put in an audit `detail`, and never
interpolated into an exception message.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from src.services.appdb.errors import AppDatabaseUnconfiguredError


def _fernet() -> Fernet:
    # Lazy `settings` import: this module is re-exported from the package __init__, and
    # `src.config` imports `appdb.config` — the same cycle-dodge `storage/accessor.py` uses.
    from src.config import settings

    if settings.app_db is None:
        raise AppDatabaseUnconfiguredError(
            "per-project databases are not configured: set APP_DB__MAINTENANCE_DSN and "
            "APP_DB__ENCRYPTION_KEY"
        )
    return Fernet(settings.app_db.encryption_key.get_secret_value())


def encrypt_password(plain: str) -> str:
    """Fernet token (urlsafe base64 text) for the registry column."""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_password(token: str) -> str:
    """The plaintext behind a stored Fernet token.

    Raises `cryptography.fernet.InvalidToken` if the key no longer matches the ciphertext
    (a rotated key with no re-encryption pass) — a loud failure, deliberately not a `None`
    that would silently assemble a DSN with an empty password.
    """
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
