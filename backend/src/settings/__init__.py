"""Role-scoped application settings (U23/U24, ADR-0029 §9).

ONE FILE PER PROCESS. To find out what a process needs in order to boot, open the file named after
it — `api.py` or `worker.py` — and read it top to bottom. `core.py` holds only what is required of
EVERY process, so a field placed there is required of every future one, permanently.

Import `settings` from `src.config` — that is the front door, and it picks the profile for this
process from `BIAL_ROLE`. This package is the declaration.

THE FOUR TIERS. A field's requirement is spelled by its SHAPE under a section header, never by a
class name. There is no `Optional*` / `Required*` class, deliberately: the same capability is
required of one role and merely gated for another, so requiredness is a property of the ROLE that
declares it, not of the capability.

    REQUIRED                no default
                            Construction fails in EVERY environment. No ENVIRONMENT value dodges
                            it. Use when there is no deployment where absence is correct.

    REQUIRED IN PRODUCTION  `X | None = None` + a `_require_<field>_in_production` validator
                            Dev and test boot without it; production refuses to start and the
                            message names the variables to set.

    FEATURE SWITCH          `X | None = None`, no validator
                            Unset means the feature is OFF — a legitimate state in EVERY
                            environment, production included. Not a weaker version of the tier
                            above; a different thing entirely.

    KNOB                    a working default
                            Set only to change behaviour.

WHICH BLOCKS EACH ROLE READS

    env prefix           api.py                      worker.py
    ------------------   -------------------------   -------------------------
    ENVIRONMENT          REQUIRED (core)             REQUIRED (core)
    DATABASE_URL         REQUIRED (core)             REQUIRED (core)
    APPS_BASE_URL        REQUIRED (core)             REQUIRED (core)
    AUTH__               REQUIRED                    --
    SUPERADMIN_EMAILS    REQUIRED                    --
    SUPPORT_CONTACT_EMAIL REQUIRED                   --
    OBJECT_STORE__       required in production      REQUIRED
    REDIS__              required in production      REQUIRED
    SANDBOX__            required in production      REQUIRED
    APP_DB__             required in production      --
    DEPLOY__             feature switch              feature switch
    FOUNDRY__            feature switch              --
    GOTENBERG_URL        feature switch              --
    SPA_DIST_DIR         feature switch              --
    FRONTEND_URL         knob (https:// gated in production)
    DAILY_TOKEN_LIMIT    knob                        --
    DB_AUTH_MODE         knob (core)                 knob (core)
    DB_ENTRA_CLIENT_ID   knob (core)                 knob (core)

The three capitalised REQUIREDs in the worker column are the reason this split exists: a worker
that boots without them can delete the Azure fleet. `worker.py` explains each one on the field.

NAMING RULE. A settings class is `<Owner>Settings` in `<owner>.py`, one per file — a future
provisioning role gets `ProvisionerSettings` / `provisioner.py` for free. A nested env block is
`<Service>Config`, declared beside the service that owns it in `src/services/<service>/config.py`
(`foundry.py` here is the documented exception). The words `Optional`, `Required`, `Surface` and
`Mixin` never appear in a class name.
"""

from src.settings.api import ApiSettings as ApiSettings
from src.settings.core import ROLE_ENV_VAR as ROLE_ENV_VAR
from src.settings.core import SETTINGS_CONFIG as SETTINGS_CONFIG
from src.settings.core import CoreSettings as CoreSettings
from src.settings.foundry import FoundryConfig as FoundryConfig
from src.settings.worker import WorkerSettings as WorkerSettings
