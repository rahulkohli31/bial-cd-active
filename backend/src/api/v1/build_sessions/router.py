"""Build-sessions HTTP router — the frozen C3 control surface (stub).

Stage-0 stub: the router mounts under `/v1/build-sessions` with NO routes yet.
Track SESSION-API fills it in Wave 1 (start/stop/status, the five lock ops, and the
GET-SSE progress feed carrying the C7 envelope) — copying the `claude/router.py` SSE
framing rather than editing the shared chat relay (D6). Mounting the empty router now
means no Wave-1 track has to re-edit the v1 aggregator.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/build-sessions", tags=["build_sessions"])
