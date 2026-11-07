"""LangChain `VectorStore` implementation backed by zvec.

zvec is an embedded, in-process vector engine (Apache-2.0) that stores the
index on local disk — no server, no Docker, no port. This module exposes it
through LangChain's `VectorStore` interface so retriever chains and agents can
use it like any other LangChain vector store.

Retrieval is hybrid: dense vectors (HNSW) fused with full-text search (FTS)
using Reciprocal Rank Fusion. Dense alone misses exact tokens like "PP-03" or
"Rp 5.000"; FTS alone misses paraphrases. RRF merges both rankings without
needing comparable score scales.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


def _zvec() -> Any:
    """Import zvec with a clear error message when the extra is missing."""
    try:
        import zvec
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "zvec is not installed. Install the agent extras: "
            "uv pip install zvec fastembed langchain langgraph"
        ) from exc
    return zvec


class ZvecVectorStore:
    """A LangChain-compatible vector store backed by a local zvec collection.

    Implements the subset of the LangChain `VectorStore` protocol that agents
    and retrievers actually use: `add_texts`, `similarity_search`,
    `similarity_search_with_score`, plus `delete`.
    """

    def __init__(
        self,
        path: str | Path,
        collection: str,
        embeddings: Any,
        dim: int,
    ) -> None:
        self.path = Path(path)
        self.collection_name = collection
        self.embeddings = embeddings
        self.dim = dim
        self._collection: Any = None
        self._lock = threading.Lock()

    # ── collection lifecycle ──────────────────────────────────────────────
    @property
    def collection(self) -> Any:
        if self._collection is None:
            with self._lock:
                if self._collection is None:
                    self._collection = self._open_existing()
        return self._collection

    def _open_existing(self) -> Any:
        zvec = _zvec()
        try:
            return zvec.open(str(self.path))
        except Exception as exc:
            raise RuntimeError(
                f"Vector collection '{self.collection_name}' does not exist at "
                f"{self.path}. Run `python -m src.main kb-build` first."
            ) from exc

    @classmethod
    def create(
        cls,
        path: str | Path,
        collection: str,
        embeddings: Any,
        dim: int,
    ) -> "ZvecVectorStore":
        """Create (or replace) a collection and return the store for it."""
        zvec = _zvec()
        target = Path(path)
        if target.exists():
            _destroy_collection(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        schema = zvec.CollectionSchema(
            name=collection,
            vectors=zvec.VectorSchema("embedding", zvec.DataType.VECTOR_FP32, dim),
            fields=[
                zvec.FieldSchema("text", zvec.DataType.STRING),
                zvec.FieldSchema("kind", zvec.DataType.STRING),
                zvec.FieldSchema("source", zvec.DataType.STRING),
                zvec.FieldSchema("metadata", zvec.DataType.STRING),
            ],
        )
        handle = zvec.create_and_open(str(target), schema)
        handle.create_index("embedding", zvec.HnswIndexParam())
        # Full-text index over the document body powers the lexical half of
        # hybrid search (exact ids, amounts and Indonesian keywords).
        handle.create_index("text", zvec.FtsIndexParam())
        handle.flush()
        store = cls(target, collection, embeddings, dim)
        store._collection = handle
        return store

    @classmethod
    def from_documents(
        cls,
        documents: Sequence[Any],
        embeddings: Any,
        path: str | Path,
        collection: str,
        dim: int,
    ) -> "ZvecVectorStore":
        store = cls.create(path, collection, embeddings, dim)
        store.add_documents(documents)
        return store

    # ── writes ────────────────────────────────────────────────────────────
    def add_documents(self, documents: Sequence[Any]) -> list[str]:
        """Index documents. Accepts LangChain Documents or plain dicts.

        A plain dict uses the keys `page_content` and `metadata`, which is how
        the corpus builder hands over its documents.
        """
        texts: list[str] = []
        metadatas: list[dict] = []
        for index, doc in enumerate(documents):
            if isinstance(doc, dict):
                text = str(doc.get("page_content") or "")
                meta = dict(doc.get("metadata") or {})
            else:
                text = str(getattr(doc, "page_content", "") or "")
                meta = dict(getattr(doc, "metadata", {}) or {})
            texts.append(text)
            metadatas.append(meta)
        ids = [
            str(metadatas[index].get("id") or f"doc-{index}")
            for index in range(len(documents))
        ]
        return self.add_texts(texts, metadatas=metadatas, ids=ids)

    def add_texts(
        self,
        texts: Sequence[str],
        metadatas: Sequence[dict] | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[str]:
        """Insert documents. Re-inserting an existing id replaces it."""
        items = [str(text or "") for text in texts]
        if not items:
            return []
        zvec = _zvec()
        metas = [dict(meta or {}) for meta in (metadatas or [{}] * len(items))]
        doc_ids = [str(value) for value in (ids or [f"doc-{i}" for i in range(len(items))])]
        vectors = self.embeddings.embed_documents(items)
        docs = []
        for index, text in enumerate(items):
            meta = metas[index]
            docs.append(
                zvec.Doc(
                    id=doc_ids[index],
                    vectors={"embedding": vectors[index]},
                    fields={
                        "text": text,
                        "kind": str(meta.get("kind") or "note"),
                        "source": str(meta.get("source") or ""),
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    },
                )
            )
        self.collection.upsert(docs)
        self.collection.flush()
        return doc_ids

    def delete(self, ids: Sequence[str]) -> bool:
        """Remove documents by id. Unknown ids are ignored by zvec."""
        targets = [str(value) for value in ids]
        if not targets:
            return False
        self.collection.delete(targets)
        self.collection.flush()
        return True

    # ── reads ─────────────────────────────────────────────────────────────
    def similarity_search(
        self,
        query: str,
        k: int = 4,
        filter: str | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        return [
            doc for doc, _score in self.similarity_search_with_score(query, k, filter, **kwargs)
        ]

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        filter: str | None = None,
        **kwargs: Any,
    ) -> list[tuple[Any, float]]:
        """Hybrid dense+FTS search. Returns (document, score) pairs, best first.

        `filter` is a zvec boolean expression over scalar fields, e.g.
        ``"kind = 'rule'"``.
        """
        zvec = _zvec()
        text = str(query or "").strip()
        if not text:
            return []
        vector = self.embeddings.embed_query(text)
        if not vector:
            return []

        queries = [zvec.Query(field_name="embedding", vector=vector)]
        weights = [1.0]
        # FTS needs a safe match string; an empty or symbolic query would raise.
        match = _fts_match_string(text)
        if match:
            queries.append(zvec.Query(field_name="text", fts=zvec.Fts(match_string=match)))
            # Dense carries most of the signal; lexical search is a tie-breaker
            # for exact tokens ("PP-03", "Rp 5.000") that dense may blur.
            weights = [0.7, 0.3]

        try:
            results = self.collection.query(
                queries=queries,
                topk=max(1, int(k)),
                filter=filter,
                reranker=zvec.WeightedReRanker(weights),
            )
        except Exception as exc:
            logger.warning("Hybrid zvec query failed, retrying dense-only: %s", exc)
            results = self.collection.query(
                queries=zvec.Query(field_name="embedding", vector=vector),
                topk=max(1, int(k)),
                filter=filter,
            )
        return [(self._to_document(row), float(row.score or 0.0)) for row in results]

    def as_retriever(self, search_kwargs: dict[str, Any] | None = None) -> Any:
        """Return a LangChain retriever over this store."""
        from langchain_core.retrievers import BaseRetriever

        options = dict(search_kwargs or {})
        store = self

        class _ZvecRetriever(BaseRetriever):
            def _get_relevant_documents(self, query: str, **kwargs: Any) -> list[Any]:
                return store.similarity_search(query, **options)

        return _ZvecRetriever()

    # ── helpers ──────────────────────────────────────────────────────────
    def _to_document(self, row: Any) -> Any:
        from langchain_core.documents import Document

        fields = dict(getattr(row, "fields", {}) or {})
        raw_meta = fields.get("metadata") or "{}"
        try:
            meta = json.loads(raw_meta)
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("kind", fields.get("kind") or "note")
        if fields.get("source"):
            meta.setdefault("source", fields["source"])
        meta.setdefault("id", getattr(row, "id", ""))
        return Document(page_content=str(fields.get("text") or ""), metadata=meta)

    def count(self) -> int:
        """Number of documents in the collection."""
        try:
            stats = self.collection.stats
        except Exception:
            return 0
        if isinstance(stats, dict):
            for key in ("num_docs", "doc_count", "count"):
                if isinstance(stats.get(key), int):
                    return int(stats[key])
            return 0
        for attribute in ("doc_count", "num_docs", "count"):
            value = getattr(stats, attribute, None)
            if isinstance(value, int):
                return value
        return 0


def _destroy_collection(path: Path) -> None:
    """Delete an existing collection directory so it can be recreated."""
    import shutil
    zvec = _zvec()

    try:
        col = zvec.open(str(path))
        col.destroy()
        return
    except Exception:
        pass

    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - filesystem edge case
        logger.warning("Could not remove old collection %s: %s", path, exc)


def _fts_match_string(text: str) -> str:
    """Reduce free text to a safe FTS match string.

    zvec's FTS parser rejects stray punctuation, so we keep alphanumeric
    tokens and drop everything that could break the expression grammar.
    """
    stopwords = {
        "di", "ke", "dari", "dan", "yang", "ini", "itu", "untuk", "pada", "adalah",
        "dengan", "cara", "apa", "bagaimana", "kapan", "siapa", "mengapa", "kenapa",
        "bisa", "tidak", "ada", "saya", "kamu", "aku", "dia", "mereka", "kita",
        "kalau", "jika", "atau", "ya", "halo", "hai",
    }
    raw_tokens: list[str] = []
    for raw in str(text or "").split():
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")
        if cleaned:
            raw_tokens.append(cleaned)
    filtered = [t for t in raw_tokens if t.lower() not in stopwords]
    tokens = filtered or raw_tokens
    return " ".join(tokens[:24])


def is_available() -> bool:
    """True when the optional zvec extra is importable."""
    import importlib.util

    try:
        return importlib.util.find_spec("zvec") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def iter_stored(store: ZvecVectorStore) -> Iterable[Any]:
    """Yield every stored document (used by rebuild and diagnostics)."""
    try:
        for row in store.collection.iter_docs():
            yield store._to_document(row)
    except Exception as exc:
        logger.warning("Could not iterate stored documents: %s", exc)
