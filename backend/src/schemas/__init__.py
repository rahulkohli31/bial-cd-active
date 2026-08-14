"""Cross-domain schema primitives: the single camelCase base (`CamelModel`) every
API model subclasses, and the reusable OpenAPI error-response models + `responses=`
builder. Public surface via explicit `from .x import Y as Y` re-exports
(`.claude/rules/modules.md` — never `__all__`)."""

from src.schemas.base import CamelModel as CamelModel
from src.schemas.projects import ProjectCreate as ProjectCreate
from src.schemas.projects import ProjectListResponse as ProjectListResponse
from src.schemas.projects import ProjectPatch as ProjectPatch
from src.schemas.projects import ProjectResponse as ProjectResponse
from src.schemas.responses import ADMIN_AUTH as ADMIN_AUTH
from src.schemas.responses import AUTH_401 as AUTH_401
from src.schemas.responses import AUTH_403_SUSPENDED as AUTH_403_SUSPENDED
from src.schemas.responses import DailyTokenLimitBody as DailyTokenLimitBody
from src.schemas.responses import DetailBody as DetailBody
from src.schemas.responses import ErrorEnvelope as ErrorEnvelope
from src.schemas.responses import OkResponse as OkResponse
from src.schemas.responses import error_responses as error_responses
