from unittest.mock import patch

from aios_app.rag.ingest_worker import initialize_backend
from aios_app.rag.rag_config import RagConfig


class FakeEmbedder:
    dim = 3

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeStore:
    pass


def test_initialize_backend_warms_embedder_and_store():
    cfg = RagConfig()
    embedder = FakeEmbedder()
    store = FakeStore()

    with patch("aios_app.rag.ingest_worker._get_embedder", return_value=embedder), patch(
        "aios_app.rag.ingest_worker._get_store", return_value=store
    ) as get_store:
        returned_embedder, returned_store = initialize_backend(cfg, warmup=True)

    assert returned_embedder is embedder
    assert returned_store is store
    assert embedder.calls == 1
    get_store.assert_called_once_with(cfg, vector_dim=3)


def test_initialize_backend_can_skip_repeat_warmup():
    cfg = RagConfig()
    embedder = FakeEmbedder()
    store = FakeStore()

    with patch("aios_app.rag.ingest_worker._get_embedder", return_value=embedder), patch(
        "aios_app.rag.ingest_worker._get_store", return_value=store
    ):
        initialize_backend(cfg, warmup=False)

    assert embedder.calls == 0
