"""Runner-token verify (U3/U4): a signed session JWT decoded through the X-App-Key data
chain's login-required re-check. The dedicated `mint_runner_token` wrapper was retired with
the open-sandbox pivot (its mint endpoint is gone); tokens are minted inline via
`mint_session_jwt`, so the token here is produced that way and only the verify path is asserted.
"""

from __future__ import annotations

import uuid

from src.config import settings
from src.services.appserving.runner_token import verify_runner_token
from src.services.auth.session_jwt import mint_session_jwt

_TTL = settings.auth.access_ttl_seconds


def test_verify_roundtrips_identity() -> None:
    user_id = uuid.uuid7()
    token = mint_session_jwt(user_id, 3, _TTL)
    claims = verify_runner_token(token)
    assert claims.user_id == user_id
    assert claims.token_version == 3
