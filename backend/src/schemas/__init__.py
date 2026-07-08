"""Cross-domain schema primitives: the single camelCase base (`CamelModel`) every
API model subclasses, and the reusable OpenAPI error-response models + `responses=`
builder. Public surface via explicit `from .x import Y as Y` re-exports
(`.claude/rules/modules.md` — never `__all__`)."""

from src.schemas.base import CamelModel as CamelModel
from src.schemas.responses import DailyTokenLimitBody as DailyTokenLimitBody
from src.schemas.responses import DetailBody as DetailBody
from src.schemas.responses import ErrorEnvelope as ErrorEnvelope
from src.schemas.responses import OkResponse as OkResponse
from src.schemas.responses import error_responses as error_responses
