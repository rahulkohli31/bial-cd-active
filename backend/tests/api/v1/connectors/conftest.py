"""Fixtures shared by the connector route suites: a signed-in citizen with a CSRF token, and the
registry's one key — kept here rather than imported across sibling test modules.
"""

from __future__ import annotations

from src.config import settings
from src.core.connectors import CONNECTORS
from src.db.models.user import User
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt

# The registry's only key, taken from the catalogue rather than written down. The connector's own
# name is kept out of every identifier in this tree, and a literal here would be a second place the
# key is spelled — which is how a test starts passing against a key nothing serves.
KEY = next(iter(CONNECTORS))
UNKNOWN_KEY = "no-such-system"


def auth_headers(user: User, *, with_csrf: bool = True) -> dict[str, str]:
    """Cookie session + the signed double-submit CSRF pair. `with_csrf=False` is the shape a
    cross-site form post would arrive in — the cookie rides along, the header does not."""
    jwt = mint_session_jwt(user.id, user.token_version, settings.auth.access_ttl_seconds)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}
