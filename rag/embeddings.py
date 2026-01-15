# aios_app/rag/embeddings.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, List
import hashlib

try:
    from sentence_transformers import SentenceTransformer
except Exception as e:  # pragma: no cover
    SentenceTransformer = None  # type: ignore


def stable_text_hash(text: str) -> str:
    # Hash used only for idempotency checks, not security.
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()


@dataclass
class Embedder:
    model_name: str
    device: str | None = None

    _model: "SentenceTransformer | None" = None

    def load(self) -> None:
        if self._model is not None:
            return
        if SentenceTransformer is None:
            raise RuntimeError(
                "sentence-transformers is not installed. "
                "pip install sentence-transformers"
            )
        self._model = SentenceTransformer(self.model_name, device=self.device)

    @property
    def dim(self) -> int:
        self.load()
        assert self._model is not None
        # SentenceTransformer exposes get_sentence_embedding_dimension()
        return int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.load()
        assert self._model is not None
        # normalize_embeddings=True gives cosine-friendly vectors
        vecs = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vecs.tolist()  # type: ignore
