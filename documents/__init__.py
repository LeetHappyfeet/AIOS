"""Generic document ingestion utilities."""

from .long_document import ingest_long_document, split_long_document, derive_metadata

__all__ = ["ingest_long_document", "split_long_document", "derive_metadata"]
