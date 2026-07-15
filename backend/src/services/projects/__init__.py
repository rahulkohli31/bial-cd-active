"""Project-lifecycle service helpers (cascade delete, owner-scoped resolution)."""

from src.services.projects.delete import ProjectCascadeCleanup as ProjectCascadeCleanup
from src.services.projects.delete import delete_project_cascade as delete_project_cascade
from src.services.projects.describe import bound_source as bound_source
from src.services.projects.describe import extract_source as extract_source
from src.services.projects.describe import (
    generate_project_description as generate_project_description,
)
from src.services.projects.resolve import owned_project_or_404 as owned_project_or_404
