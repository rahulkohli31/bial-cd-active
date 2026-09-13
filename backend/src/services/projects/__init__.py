"""Project-lifecycle service helpers (cascade delete, owner-scoped resolution)."""

from src.services.projects.delete import ProjectCascadeCleanup as ProjectCascadeCleanup
from src.services.projects.delete import delete_project_cascade as delete_project_cascade
from src.services.projects.delete import (
    resweep_submission_prefixes as resweep_submission_prefixes,
)
from src.services.projects.duplicates import DuplicateCheckResult as DuplicateCheckResult
from src.services.projects.duplicates import (
    find_possible_duplicates as find_possible_duplicates,
)
from src.services.projects.duplicates import log_matches_shown as log_matches_shown
from src.services.projects.duplicates import log_resolution as log_resolution
from src.services.projects.resolve import ProjectAccess as ProjectAccess
from src.services.projects.resolve import ResolvedProject as ResolvedProject
from src.services.projects.resolve import owned_project_or_404 as owned_project_or_404
from src.services.projects.resolve import (
    resolve_project_access as resolve_project_access,
)
from src.services.projects.shares import MAX_COLLEAGUE_RESULTS as MAX_COLLEAGUE_RESULTS
from src.services.projects.shares import (
    MIN_COLLEAGUE_QUERY_CHARS as MIN_COLLEAGUE_QUERY_CHARS,
)
from src.services.projects.shares import SharedProjectEntry as SharedProjectEntry
from src.services.projects.shares import ShareWithRecipient as ShareWithRecipient
from src.services.projects.shares import create_share as create_share
from src.services.projects.shares import list_shared_with_me as list_shared_with_me
from src.services.projects.shares import (
    list_shares_for_project as list_shares_for_project,
)
from src.services.projects.shares import revoke_share as revoke_share
from src.services.projects.shares import search_colleagues as search_colleagues
