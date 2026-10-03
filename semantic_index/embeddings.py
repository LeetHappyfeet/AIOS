from __future__ import annotations

from dataclasses import dataclass
from typing import List
import hashlib

SentenceTransformer = None  # Loaded only by the vector/query process.


def stable_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


@dataclass
class Embedder:
    model_name: str
    device: str | None = None
    _model: "SentenceTransformer | None" = None

    def load(self) -> None:
        if self._model is not None:
            return
        global SentenceTransformer
        if SentenceTransformer is None:
            try:
                from sentence_transformers import SentenceTransformer as model_class
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is not installed") from exc
            SentenceTransformer = model_class
        self._model = SentenceTransformer(self.model_name, device=self.device)

    @property
    def dim(self) -> int:
        self.load()
        assert self._model is not None
        return int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.load()
        assert self._model is not None
        return self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()
