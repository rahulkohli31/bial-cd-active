"""ORM model registry. Importing this package registers every model class with
`Base.metadata` so Alembic autogenerate (and the test round-trips) see the full
schema. Auth (`users`, `refresh_tokens`) lands first; token-quota and admin models
follow in later phases.
"""

from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User

__all__ = ["RefreshToken", "User"]
