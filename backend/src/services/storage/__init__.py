"""Typed Azure-Blob object-storage package behind one async interface.

Public surface, via explicit `from .x import Y as Y` re-exports. Single-tenant
(ADR-0004): keys are owner-scoped via the
`keys` builders (`attachment_key` / `app_file_key` / `assert_owned`) — badger's
multi-tenant `ScopedStorage` facade is dropped. Only setup code touches
`create_storage`/`get_storage`.
"""

from src.services.storage.accessor import aclose_storage as aclose_storage
from src.services.storage.accessor import get_storage as get_storage
from src.services.storage.accessor import reset_storage_for_tests as reset_storage_for_tests
from src.services.storage.base import ListPage as ListPage
from src.services.storage.base import ObjectMeta as ObjectMeta
from src.services.storage.base import ObjectStorage as ObjectStorage
from src.services.storage.config import AzureStorageConfig as AzureStorageConfig
from src.services.storage.config import StorageConfig as StorageConfig
from src.services.storage.errors import StorageAuthError as StorageAuthError
from src.services.storage.errors import StorageError as StorageError
from src.services.storage.errors import StorageNotFoundError as StorageNotFoundError
from src.services.storage.errors import StorageSignError as StorageSignError
from src.services.storage.errors import StorageUploadError as StorageUploadError
from src.services.storage.errors import UnsupportedCapabilityError as UnsupportedCapabilityError
from src.services.storage.factory import create_storage as create_storage
from src.services.storage.keys import app_file_key as app_file_key
from src.services.storage.keys import assert_owned as assert_owned
from src.services.storage.keys import attachment_key as attachment_key
from src.services.storage.keys import normalize_metadata as normalize_metadata
from src.services.storage.keys import owner_prefix as owner_prefix
