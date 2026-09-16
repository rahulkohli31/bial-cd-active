"""Foundry embedding wiring for hybrid search. Public surface via explicit re-exports."""

from src.services.embeddings.client import EmbedderDep as EmbedderDep
from src.services.embeddings.client import EmbeddingFoundryOnlyError as EmbeddingFoundryOnlyError
from src.services.embeddings.client import aclose_embedder as aclose_embedder
from src.services.embeddings.client import (
    assert_embedding_guard_at_startup as assert_embedding_guard_at_startup,
)
from src.services.embeddings.client import build_embedder as build_embedder
from src.services.embeddings.client import embedder_dependency as embedder_dependency
from src.services.embeddings.write import (
    write_description_embedding as write_description_embedding,
)
