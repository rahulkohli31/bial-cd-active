"""Project-lifecycle service helpers (cascade delete, write-target resolution)."""

from src.services.projects.delete import delete_project_cascade as delete_project_cascade
from src.services.projects.resolve import DEFAULT_PROJECT_NAME as DEFAULT_PROJECT_NAME
from src.services.projects.resolve import resolve_project_for_write as resolve_project_for_write
