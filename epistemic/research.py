from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence
from uuid import UUID

from aios_app.corpus import consume_corpus_sections
from aios_app.db import Database


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}")
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "was", "what", "when", "where", "which", "who", "why", "with",
})


def research_terms(text: str, *, limit: int = 24) -> tuple[str, ...]:
    """Extract stable lexical search terms without an LLM."""
    seen: set[str] = set()
    terms: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group(0)
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= limit:
            break
    return tuple(terms)


@dataclass(frozen=True)
class KnowledgeDemand:
    needed: bool
    terms: tuple[str, ...]
    missing_terms: tuple[str, ...]
    coverage: float
    reason: str


class KnowledgeDemandResolver:
    """Deterministic gate for deciding whether corpus lookup is warranted.

    The resolver is deliberately conservative: it only compares salient lexical
    concepts against knowledge already available to the character. It never
    creates a query with a model and it never writes character knowledge.
    """

    def __init__(self, *, minimum_terms: int = 1, coverage_threshold: float = 0.60):
        self.minimum_terms = max(1, int(minimum_terms))
        self.coverage_threshold = max(0.0, min(float(coverage_threshold), 1.0))

    def resolve(
        self,
        focus_text: str,
        *,
        known_texts: Iterable[str] = (),
    ) -> KnowledgeDemand:
        terms = research_terms(focus_text)
        if len(terms) < self.minimum_terms:
            return KnowledgeDemand(False, terms, (), 1.0, "insufficient_terms")

        known_tokens: set[str] = set()
        for text in known_texts:
            known_tokens.update(research_terms(str(text), limit=256))

        missing = tuple(term for term in terms if term not in known_tokens)
        coverage = 1.0 - (len(missing) / len(terms))
        needed = bool(missing) and coverage < self.coverage_threshold
        return KnowledgeDemand(
            needed=needed,
            terms=terms,
            missing_terms=missing,
            coverage=coverage,
            reason="knowledge_gap" if needed else "sufficient_coverage",
        )


@dataclass(frozen=True)
class CorpusResearchHit:
    section_id: UUID
    document_id: UUID
    score: float
    title: str | None
    heading: str | None
    excerpt: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class CorpusResearchResult:
    research_id: UUID
    instance_id: UUID
    character_id: str
    query: str
    terms: tuple[str, ...]
    hits: tuple[CorpusResearchHit, ...]
    status: str

    def reference_context(self) -> list[dict]:
        """Render temporary context without crossing the acquisition boundary."""
        return [
            {
                "kind": "corpus_reference",
                "research_id": str(self.research_id),
                "section_id": str(hit.section_id),
                "document_id": str(hit.document_id),
                "title": hit.title,
                "heading": hit.heading,
                "text": hit.excerpt,
                "score": hit.score,
                "scopes": list(hit.scopes),
                "durable_knowledge": False,
            }
            for hit in self.hits
        ]


class CorpusSearchService:
    """Character-scoped deterministic full-text search over the cold corpus."""

    def __init__(self, db: Database):
        self.db = db

    async def search(
        self,
        *,
        instance_id: UUID,
        query: str,
        limit: int = 8,
    ) -> CorpusResearchResult:
        query = query.strip()
        if not query:
            raise ValueError("corpus research query is empty")
        bounded_limit = max(1, min(int(limit), 32))
        terms = research_terms(query)

        instance = await self.db.fetchrow(
            "SELECT character_id FROM aios.character_instance WHERE instance_id=$1",
            instance_id,
        )
        if not instance:
            raise ValueError(f"unknown character instance {instance_id}")
        character_id = str(instance["character_id"])

        access = await self.db.fetchrow(
            """
            SELECT EXISTS (
                SELECT 1 FROM aios.character_corpus_access
                WHERE character_id=$1 AND allowed
            ) AS has_access
            """,
            character_id,
        )
        if not access or not access["has_access"]:
            research_id = await self._record_search(
                instance_id, character_id, query, terms, "no_access", 0
            )
            return CorpusResearchResult(
                research_id, instance_id, character_id, query, terms, (), "no_access"
            )

        rows = await self.db.fetch(
            """
            WITH q AS (
                SELECT websearch_to_tsquery('english', $2) AS query
            )
            SELECT cs.section_id, cs.document_id, cd.title, cs.heading,
                   ts_rank_cd(cs.search_vector, q.query) AS score,
                   ts_headline(
                       'english',
                       cs.content,
                       q.query,
                       'MaxWords=55, MinWords=18, ShortWord=3, HighlightAll=false'
                   ) AS excerpt,
                   array_agg(DISTINCT cds.scope_key ORDER BY cds.scope_key) AS scopes
            FROM aios.corpus_section cs
            JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
            JOIN aios.corpus_document_scope cds ON cds.document_id=cs.document_id
            CROSS JOIN q
            WHERE cs.search_vector @@ q.query
              AND EXISTS (
                  SELECT 1
                  FROM aios.corpus_document_scope allowed_scope
                  JOIN aios.character_corpus_access grant_row
                    ON grant_row.character_id=$1
                   AND grant_row.allowed
                   AND (
                        allowed_scope.scope_key=grant_row.scope_key
                        OR left(
                            allowed_scope.scope_key,
                            length(grant_row.scope_key) + 1
                        )=grant_row.scope_key || '.'
                   )
                  WHERE allowed_scope.document_id=cs.document_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM aios.corpus_document_scope denied_scope
                  JOIN aios.character_corpus_access deny_row
                    ON deny_row.character_id=$1
                   AND NOT deny_row.allowed
                   AND (
                        denied_scope.scope_key=deny_row.scope_key
                        OR left(
                            denied_scope.scope_key,
                            length(deny_row.scope_key) + 1
                        )=deny_row.scope_key || '.'
                   )
                  WHERE denied_scope.document_id=cs.document_id
              )
            GROUP BY cs.section_id, cs.document_id, cd.title, cs.heading,
                     cs.search_vector, cs.content, q.query
            ORDER BY score DESC, cs.section_id
            LIMIT $3
            """,
            character_id,
            query,
            bounded_limit,
        )

        hits = tuple(
            CorpusResearchHit(
                section_id=row["section_id"],
                document_id=row["document_id"],
                score=float(row["score"] or 0.0),
                title=row["title"],
                heading=row["heading"],
                excerpt=row["excerpt"] or "",
                scopes=tuple(row["scopes"] or ()),
            )
            for row in rows
        )
        status = "searched" if hits else "no_results"
        research_id = await self._record_search(
            instance_id, character_id, query, terms, status, len(hits)
        )
        for rank, hit in enumerate(hits, start=1):
            await self.db.execute(
                """
                INSERT INTO aios.character_corpus_exposure (
                    research_id, section_id, rank, score, meta
                )
                VALUES ($1,$2,$3,$4,$5::jsonb)
                ON CONFLICT (research_id, section_id) DO NOTHING
                """,
                research_id,
                hit.section_id,
                rank,
                hit.score,
                json.dumps({"scopes": list(hit.scopes)}),
            )

        return CorpusResearchResult(
            research_id, instance_id, character_id, query, terms, hits, status
        )

    async def _record_search(
        self,
        instance_id: UUID,
        character_id: str,
        query: str,
        terms: Sequence[str],
        status: str,
        result_count: int,
    ) -> UUID:
        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.character_research_event (
                instance_id, character_id, query_text, query_terms,
                status, result_count
            )
            VALUES ($1,$2,$3,$4::text[],$5,$6)
            RETURNING research_id
            """,
            instance_id,
            character_id,
            query,
            list(terms),
            status,
            result_count,
        )
        return row["research_id"]


class CharacterResearchService:
    """Internal character research capability.

    Search produces temporary reference context. acquire() is a separate,
    explicit transition into the existing corpus-consumption pipeline.
    """

    def __init__(self, db: Database):
        self.db = db
        self.searcher = CorpusSearchService(db)

    async def search(
        self,
        *,
        instance_id: UUID,
        query: str,
        limit: int = 8,
    ) -> CorpusResearchResult:
        return await self.searcher.search(instance_id=instance_id, query=query, limit=limit)

    async def acquire(
        self,
        *,
        instance_id: UUID,
        section_ids: Sequence[UUID],
        mode: str = "research",
    ) -> dict:
        instance = await self.db.fetchrow(
            "SELECT character_id FROM aios.character_instance WHERE instance_id=$1",
            instance_id,
        )
        if not instance:
            raise ValueError(f"unknown character instance {instance_id}")
        character_id = str(instance["character_id"])

        for section_id in section_ids:
            allowed = await self.db.fetchrow(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM aios.corpus_section cs
                    JOIN aios.corpus_document_scope ds ON ds.document_id=cs.document_id
                    JOIN aios.character_corpus_access grant_row
                      ON grant_row.character_id=$2
                     AND grant_row.allowed
                     AND (
                          ds.scope_key=grant_row.scope_key
                          OR left(ds.scope_key, length(grant_row.scope_key) + 1)
                             = grant_row.scope_key || '.'
                     )
                    WHERE cs.section_id=$1
                ) AND NOT EXISTS (
                    SELECT 1
                    FROM aios.corpus_section cs
                    JOIN aios.corpus_document_scope ds ON ds.document_id=cs.document_id
                    JOIN aios.character_corpus_access deny_row
                      ON deny_row.character_id=$2
                     AND NOT deny_row.allowed
                     AND (
                          ds.scope_key=deny_row.scope_key
                          OR left(ds.scope_key, length(deny_row.scope_key) + 1)
                             = deny_row.scope_key || '.'
                     )
                    WHERE cs.section_id=$1
                ) AS permitted
                """,
                section_id,
                character_id,
            )
            if not allowed or not allowed["permitted"]:
                raise PermissionError(
                    f"character {character_id!r} cannot acquire corpus section {section_id}"
                )

        return await consume_corpus_sections(
            self.db,
            instance_id=instance_id,
            section_ids=section_ids,
            mode=mode,
        )
