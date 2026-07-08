"""Computed-role RBAC (no stored role column, ADR-0005). Public surface via
explicit `from .x import Y as Y` re-exports."""

from src.services.rbac.roles import Role as Role
from src.services.rbac.roles import is_super_duper_admin as is_super_duper_admin
from src.services.rbac.roles import role_for as role_for
