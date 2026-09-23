from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True)
class CorpusFacet:
    facet_type: str
    facet_value: str
    source: str = "adapter"
    confidence: float = 1.0
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CorpusClassification:
    facets: tuple[CorpusFacet, ...] = ()
    epistemic_namespace: str | None = None
    metadata: dict = field(default_factory=dict)


class CorpusSourceAdapter(Protocol):
    def matches(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> bool: ...
    def classify(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> CorpusClassification: ...


def _values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


class AO3Adapter:
    """Classify AO3 from structured work tags, never from prose."""
    HOSTS = {"archiveofourown.org", "www.archiveofourown.org"}

    def matches(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> bool:
        return (urlparse(source_uri or "").hostname or "").lower() in self.HOSTS

    def classify(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> CorpusClassification:
        ao3 = metadata.get("ao3")
        ao3 = ao3 if isinstance(ao3, Mapping) else {}
        facets: list[CorpusFacet] = [
            CorpusFacet("repository", "ao3", meta={"evidence": "source_host"}),
            CorpusFacet("source_type", "fanfiction", meta={"evidence": "repository"}),
            CorpusFacet("canon_status", "fanwork", meta={"evidence": "repository"}),
        ]
        for facet_type, key in (("fandom", "fandoms"), ("character", "characters"), ("relationship", "relationships"), ("tag", "tags")):
            for raw in _values(ao3.get(key)):
                facets.append(CorpusFacet(facet_type, raw.casefold(), meta={"evidence": "work_tag", "raw_value": raw}))
        return CorpusClassification(facets=tuple(facets), epistemic_namespace="fanwork", metadata={"adapter": "ao3"})


class StructuredMetadataAdapter:
    """Classify explicit source/catalog metadata without reading document prose."""

    FIELD_MAP = (
        ("fandom", "fandom"), ("fandoms", "fandom"),
        ("franchise", "franchise"), ("franchises", "franchise"),
        ("universe", "universe"), ("universes", "universe"),
        ("subject", "subject"), ("subjects", "subject"),
        ("topic", "subject"), ("topics", "subject"),
        ("domain", "domain"), ("domains", "domain"),
    )
    CONTAINERS = ("structured", "catalog", "book", "schema_org")

    def matches(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> bool:
        return any(isinstance(metadata.get(key), Mapping) for key in self.CONTAINERS)

    def classify(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> CorpusClassification:
        facets: list[CorpusFacet] = []
        seen: set[tuple[str, str]] = set()
        for container_name in self.CONTAINERS:
            container = metadata.get(container_name)
            if not isinstance(container, Mapping):
                continue
            for field, facet_type in self.FIELD_MAP:
                for raw in _values(container.get(field)):
                    value = raw.casefold()
                    key = (facet_type, value)
                    if key in seen:
                        continue
                    seen.add(key)
                    facets.append(CorpusFacet(
                        facet_type, value,
                        meta={
                            "evidence": "source_native_structured_metadata",
                            "container": container_name,
                            "field": field,
                            "raw_value": raw,
                        },
                    ))
            source_type = _values(container.get("source_type") or container.get("type"))
            for raw in source_type:
                key = ("source_type", raw.casefold())
                if key not in seen:
                    seen.add(key)
                    facets.append(CorpusFacet(
                        "source_type", raw.casefold(),
                        meta={"evidence": "source_native_structured_metadata",
                              "container": container_name},
                    ))
        return CorpusClassification(
            facets=tuple(facets),
            metadata={"adapter": "structured-metadata"},
        )


class GenericWebAdapter:
    def matches(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> bool:
        return True

    def classify(self, *, source_uri: str | None, metadata: Mapping[str, object]) -> CorpusClassification:
        host = (urlparse(source_uri or "").hostname or "").lower()
        facets = (CorpusFacet("repository", host, meta={"evidence": "source_host"}),) if host else ()
        return CorpusClassification(facets=facets, metadata={"adapter": "generic-web"})


ADAPTERS: tuple[CorpusSourceAdapter, ...] = (AO3Adapter(), StructuredMetadataAdapter(), GenericWebAdapter())


def classify_corpus_document(*, source_uri: str | None, metadata: Mapping[str, object] | None = None) -> CorpusClassification:
    values = metadata or {}
    for adapter in ADAPTERS:
        if adapter.matches(source_uri=source_uri, metadata=values):
            return adapter.classify(source_uri=source_uri, metadata=values)
    return CorpusClassification()
