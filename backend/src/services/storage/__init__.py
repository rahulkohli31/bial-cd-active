"""Typed Azure-Blob object-storage package behind one async interface.

Public surface, via explicit `from .x import Y as Y` re-exports. Single-tenant
(ADR-0004): keys are owner-scoped via the
`keys` builders (`attachment_key` / `app_file_key` / `assert_owned`) — badger's
multi-tenant `ScopedStorage` facade is dropped. Only setup code touches
`create_storage`/`get_storage`.
"""

from src.services.storage.accessor import aclose_storage as aclose_storage
from src.services.storage.accessor import get_app_container_store as get_app_container_store
from src.services.storage.accessor import get_storage as get_storage
from src.services.storage.accessor import reset_storage_for_tests as reset_storage_for_tests
from src.services.storage.app_containers import APP_CONTAINER_SAS_TTL as APP_CONTAINER_SAS_TTL
from src.services.storage.app_containers import AppContainerStore as AppContainerStore
from src.services.storage.base import ListPage as ListPage
from src.services.storage.base import ObjectMeta as ObjectMeta
from src.services.storage.base import ObjectStorage as ObjectStorage
from src.services.storage.bundle import BUNDLE_CONTENT_TYPE as BUNDLE_CONTENT_TYPE
from src.services.storage.bundle import BundleValidationError as BundleValidationError
from src.services.storage.bundle import parse_bundle_head_sha as parse_bundle_head_sha
from src.services.storage.config import AzureStorageConfig as AzureStorageConfig
from src.services.storage.config import StorageConfig as StorageConfig
from src.services.storage.errors import StorageAuthError as StorageAuthError
from src.services.storage.errors import StorageError as StorageError
from src.services.storage.errors import StorageNotFoundError as StorageNotFoundError
from src.services.storage.errors import StorageSignError as StorageSignError
from src.services.storage.errors import StorageUnconfiguredError as StorageUnconfiguredError
from src.services.storage.errors import StorageUploadError as StorageUploadError
from src.services.storage.errors import UnsupportedCapabilityError as UnsupportedCapabilityError
from src.services.storage.factory import create_storage as create_storage
from src.services.storage.keys import SNAPSHOT_HEAD_METADATA_KEY as SNAPSHOT_HEAD_METADATA_KEY
from src.services.storage.keys import app_file_key as app_file_key
from src.services.storage.keys import assert_owned as assert_owned
from src.services.storage.keys import attachment_key as attachment_key
from src.services.storage.keys import container_name as container_name
from src.services.storage.keys import divert_key as divert_key
from src.services.storage.keys import divert_prefix as divert_prefix
from src.services.storage.keys import head_sha_from_metadata as head_sha_from_metadata
from src.services.storage.keys import normalize_metadata as normalize_metadata
from src.services.storage.keys import owner_prefix as owner_prefix
from src.services.storage.keys import quarantine_key as quarantine_key
from src.services.storage.keys import quarantine_prefix as quarantine_prefix
from src.services.storage.keys import recovery_key as recovery_key
from src.services.storage.keys import snapshot_key as snapshot_key
from src.services.storage.keys import submission_key as submission_key
from src.services.storage.keys import submissions_prefix as submissions_prefix
from src.services.storage.listing import all_keys_under as all_keys_under
from src.services.storage.snapshot_read import ExtractedSnapshot as ExtractedSnapshot
from src.services.storage.snapshot_read import NoAppYet as NoAppYet
from src.services.storage.snapshot_read import SnapshotExtractionError as SnapshotExtractionError
from src.services.storage.snapshot_read import extract_snapshot as extract_snapshot
from src.services.storage.snapshot_read import sweep_extractions as sweep_extractions
from src.services.storage.sweep import sweep_app_containers as sweep_app_containers
from src.services.storage.sweep import sweep_blobs as sweep_blobs
