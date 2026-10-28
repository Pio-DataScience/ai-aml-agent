"""Lazy, injectable embedding adapters for the DWH scenario package."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
from typing import Any, Protocol

from services.aml_builder.web.services.settings import Settings, settings


class EmbeddingAdapter(Protocol):
    """Minimal synchronous embedding interface shared by adapters."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class EmbeddingIdentity:
    """Index compatibility identity for an embedding configuration."""

    provider: str
    model: str
    dimension: int
    fingerprint: str


_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_CACHE_LOCK = threading.Lock()
_LOCAL_INFERENCE_LOCK = threading.BoundedSemaphore(value=1)


def embedding_identity(config: Settings = settings) -> EmbeddingIdentity:
    """Build a stable identity used to reject incompatible vector indexes."""
    provider = config.DWH_SQL_EMBEDDINGS_PROVIDER.casefold()
    model = config.DWH_SQL_EMBEDDINGS_MODEL
    fingerprint = (
        config.DWH_SQL_EMBEDDINGS_FINGERPRINT
        or hashlib.sha256(
            f"{provider}:{model}:{config.DWH_SQL_EMBEDDINGS_DIMENSION}".encode()
        ).hexdigest()
    )
    return EmbeddingIdentity(
        provider, model, config.DWH_SQL_EMBEDDINGS_DIMENSION, fingerprint
    )


class EmbeddingProviderManager:
    """Create provider adapters on demand and cache model instances by device."""

    def __init__(self, config: Settings = settings) -> None:
        """Store configuration without loading models or contacting providers."""
        self.config = config
        self.identity = embedding_identity(config)

    def get(self) -> EmbeddingAdapter:
        """Return the configured adapter, initializing optional clients lazily."""
        provider = self.identity.provider
        model = self.identity.model
        device = self.config.DWH_SQL_EMBEDDINGS_DEVICE
        cache_key = (provider, model, device)
        with _CACHE_LOCK:
            if cache_key not in _MODEL_CACHE:
                _MODEL_CACHE[cache_key] = self._create(provider, model, device)
            return _MODEL_CACHE[cache_key]

    def _create(self, provider: str, model: str, device: str) -> EmbeddingAdapter:
        """Construct one adapter, raising clear errors for missing optional extras."""
        if provider == "openai":
            from langchain_openai import OpenAIEmbeddings

            api_key = self.config.DWH_SQL_OPENAI_API_KEY or self.config.OPENAI_API_KEY
            if not api_key:
                raise ValueError("OPENAI_API_KEY is required for OpenAI embeddings")
            return OpenAIEmbeddings(
                model=model,
                api_key=api_key,
                dimensions=self.identity.dimension,
                request_timeout=self.config.DWH_SQL_READ_TIMEOUT_SECONDS,
                max_retries=min(
                    self.config.DWH_SQL_RETRY_BUDGET,
                    self.config.DWH_SQL_MAX_ATTEMPTS - 1,
                ),
            )
        if provider == "cohere":
            try:
                from langchain_cohere import CohereEmbeddings
            except ImportError as exc:
                raise ImportError(
                    "Cohere embeddings require the optional 'cohere' extra"
                ) from exc
            if not self.config.DWH_SQL_COHERE_API_KEY:
                raise ValueError(
                    "DWH_SQL_COHERE_API_KEY is required for Cohere embeddings"
                )
            return CohereEmbeddings(
                model=model,
                cohere_api_key=self.config.DWH_SQL_COHERE_API_KEY,
                request_timeout=self.config.DWH_SQL_READ_TIMEOUT_SECONDS,
                max_retries=min(
                    self.config.DWH_SQL_RETRY_BUDGET,
                    self.config.DWH_SQL_MAX_ATTEMPTS - 1,
                ),
            )
        if provider in {"local_bge", "local_finance"}:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "Local embeddings require the optional 'local-embeddings' extra"
                ) from exc
            try:
                return SentenceTransformer(model, device=device)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(
                    f"Could not load configured local embedding model '{model}'"
                ) from exc
        raise ValueError(f"Unsupported embedding provider: {provider}")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed text batches, serializing access to local model instances."""
        if not texts:
            return []
        adapter = self.get()
        if self.identity.provider.startswith("local_"):
            with _LOCAL_INFERENCE_LOCK:
                vectors = adapter.encode(texts, convert_to_numpy=True)
            result = vectors.tolist()
        else:
            result = adapter.embed_documents(texts)
        self._validate_vectors(result, expected_count=len(texts))
        return result

    def embed_query(self, text: str) -> list[float]:
        """Embed one query using the same configured model and cache key."""
        if self.identity.provider.startswith("local_"):
            return self.embed_documents([text])[0]
        vector = self.get().embed_query(text)
        self._validate_vectors([vector], expected_count=1)
        return vector

    def _validate_vectors(
        self, vectors: list[list[float]], *, expected_count: int
    ) -> None:
        """Reject provider output that cannot match the persisted Chroma dimension."""
        if len(vectors) != expected_count or any(
            len(vector) != self.identity.dimension for vector in vectors
        ):
            raise ValueError(
                "Embedding output dimension does not match configured index identity "
                f"({self.identity.dimension})"
            )
