"""Local sentence embeddings backed by fastembed (ONNX runtime).

Why local: the bot already runs on the user's machine and the vector store is
embedded on disk. Pulling embeddings from a hosted API would add a network
round-trip per retrieval and another secret to manage. The model is downloaded
once by fastembed and then cached on disk.

The wrapper implements LangChain's `Embeddings` interface so it plugs into any
LangChain vector store or retriever without extra glue.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


def _normalize(vector: Any) -> list[float]:
    """L2-normalize a vector so inner-product == cosine similarity.

    zvec's HNSW index uses inner product by default. Without normalization the
    score depends on vector magnitude, which makes a relevance threshold
    meaningless and lets long documents outrank better-matching short ones.
    """
    values = [float(value) for value in vector]
    norm = sum(value * value for value in values) ** 0.5
    if norm <= 0:
        return values
    return [value / norm for value in values]


class LocalEmbeddings:
    """LangChain-compatible embedding provider (implements the protocol lazily).

    `langchain_core.embeddings.Embeddings` is imported lazily so that the rest
    of the bot keeps working even when the optional agent extras are missing.
    """

    def __init__(
        self,
        model_name: str,
        cache_dir: str | None = None,
        threads: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.threads = threads
        self._model: Any = None
        self._lock = threading.Lock()

    # ── lazy model loading ────────────────────────────────────────────────
    def _load(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    kwargs: dict[str, Any] = {"model_name": self.model_name}
                    if self.cache_dir:
                        kwargs["cache_dir"] = self.cache_dir
                    if self.threads:
                        kwargs["threads"] = self.threads
                    logger.info("Loading embedding model %s", self.model_name)
                    self._model = TextEmbedding(**kwargs)
        return self._model

    @property
    def model(self) -> Any:
        return self._load()

    # ── embedding API ─────────────────────────────────────────────────────
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        items = [str(text or "") for text in texts]
        if not items:
            return []
        model = self._load()
        return [_normalize(vector) for vector in model.embed(items)]

    def embed_query(self, text: str) -> list[float]:
        result = self.embed_documents([text])
        return result[0] if result else []

    # Alias used by some LangChain internals.
    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        return self.embed_documents(list(texts))


def build_embeddings(
    model_name: str | None = None,
    cache_dir: str | None = None,
) -> LocalEmbeddings:
    """Create the embedding provider used by the agent and the indexer."""
    from .. import config

    return LocalEmbeddings(
        model_name=model_name or config.EMBEDDING_MODEL,
        cache_dir=cache_dir,
    )


def is_available() -> bool:
    """True when the optional embedding extra is importable."""
    import importlib.util

    try:
        return importlib.util.find_spec("fastembed") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def as_langchain_embeddings(provider: LocalEmbeddings) -> Any:
    """Return the provider as a real `langchain_core` Embeddings instance."""
    from langchain_core.embeddings import Embeddings

    class _Adapter(Embeddings):
        def __init__(self, inner: LocalEmbeddings) -> None:
            self._inner = inner

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return self._inner.embed_documents(texts)

        def embed_query(self, text: str) -> list[float]:
            return self._inner.embed_query(text)

    return _Adapter(provider)
