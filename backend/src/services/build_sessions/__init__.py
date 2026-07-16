"""Build-session coordination service (contracts C5 / C7 / C9 / C4).

The one-per-user Redis lock + heartbeat + registry-state helpers (`locks`), the reaper
ordering + reconciliation sweep (`reaper`), the C9 app-data credential mint + injection
(`appdata`), the C4 snapshot write (`snapshot`), and the in-process session lifecycle +
progress channel + BRAIN launch (`manager`). Public surface via explicit
`from .x import Y as Y` re-exports (`.claude/rules/modules.md` — never `__all__`).
"""

from src.services.build_sessions.appdata import build_app_env as build_app_env
from src.services.build_sessions.appdata import resolve_app_for_project as resolve_app_for_project
from src.services.build_sessions.appstorage import provision_app_storage as provision_app_storage
from src.services.build_sessions.locks import acquire_lock as acquire_lock
from src.services.build_sessions.locks import delete_registry as delete_registry
from src.services.build_sessions.locks import heartbeat_is_alive as heartbeat_is_alive
from src.services.build_sessions.locks import lock_expires_at as lock_expires_at
from src.services.build_sessions.locks import lock_is_held as lock_is_held
from src.services.build_sessions.locks import mark_registry_ending as mark_registry_ending
from src.services.build_sessions.locks import read_registry as read_registry
from src.services.build_sessions.locks import reap_lock as reap_lock
from src.services.build_sessions.locks import release_lock_as_holder as release_lock_as_holder
from src.services.build_sessions.locks import renew_lock as renew_lock
from src.services.build_sessions.locks import write_heartbeat as write_heartbeat
from src.services.build_sessions.manager import BuildSession as BuildSession
from src.services.build_sessions.manager import (
    BuildSessionConflictError as BuildSessionConflictError,
)
from src.services.build_sessions.manager import SessionManager as SessionManager
from src.services.build_sessions.manager import (
    SnapshotUnavailableError as SnapshotUnavailableError,
)
from src.services.build_sessions.manager import app_name_for as app_name_for
from src.services.build_sessions.manager import get_session_manager as get_session_manager
from src.services.build_sessions.manager import (
    reset_session_manager_for_tests as reset_session_manager_for_tests,
)
from src.services.build_sessions.manager import (
    set_session_manager_for_tests as set_session_manager_for_tests,
)
from src.services.build_sessions.reaper import reap_user as reap_user
from src.services.build_sessions.reaper import reconcile_user as reconcile_user
from src.services.build_sessions.reaper import sweep_all as sweep_all
from src.services.build_sessions.snapshot import write_snapshot as write_snapshot
