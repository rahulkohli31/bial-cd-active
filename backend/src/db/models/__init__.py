"""ORM model registry. Importing this package registers every model class with
`Base.metadata` so Alembic autogenerate (and the test round-trips) see the full
schema. Auth (`users`, `refresh_tokens`) lands first; token-quota and admin models
follow in later phases.
"""

from src.db.models.app_registry import AppRegistry as AppRegistry
from src.db.models.attachment import Attachment as Attachment
from src.db.models.audit import AuditLog as AuditLog
from src.db.models.clear_data_token import ClearDataToken as ClearDataToken
from src.db.models.conversation import Conversation as Conversation
from src.db.models.data_record import DataRecord as DataRecord
from src.db.models.feedback import Feedback as Feedback
from src.db.models.message import Message as Message
from src.db.models.project import Project as Project
from src.db.models.project_database import ProjectDatabase as ProjectDatabase
from src.db.models.refresh_token import RefreshToken as RefreshToken
from src.db.models.token_usage import TokenUsage as TokenUsage
from src.db.models.user import User as User
from src.db.models.user_limit import UserLimit as UserLimit
