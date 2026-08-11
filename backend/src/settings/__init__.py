"""Role-scoped application settings (U23, ADR-0029 §9).

`core` holds what every process needs; `capabilities` holds one mixin per capability;
`profiles` composes them into `ApiSettings` and `WorkerSettings` and picks one by role.

Import `settings` from `src.config` — this package is the machinery, not the front door.
"""

from src.settings.capabilities import FoundryConfig as FoundryConfig
from src.settings.core import ROLE_ENV_VAR as ROLE_ENV_VAR
from src.settings.core import SETTINGS_CONFIG as SETTINGS_CONFIG
from src.settings.core import CoreSettings as CoreSettings
from src.settings.core import Role as Role
from src.settings.profiles import ApiSettings as ApiSettings
from src.settings.profiles import WorkerSettings as WorkerSettings
from src.settings.profiles import build_profile as build_profile
