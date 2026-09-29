from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence
from uuid import UUID

from aios_app.corpus import consume_corpus_sections
from aios_app.db import Database
from aios_app.epistemic.weights import get_profile


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


@dataclass(frozen=True)
class RetrievalDemand:
    """A conservative routing hint for generation-time knowledge retrieval."""

    route: str
    focus_text: str
    reason: str
    kind: str = "general"
    subject: str = ""
    topic_terms: tuple[str, ...] = ()
    temporal_scope: str = "unspecified"


@dataclass(frozen=True)
class RetrievalEvidenceStatus:
    """Query-relative support carried to generation without becoming knowledge."""

    route: str
    status: str
    demand_kind: str
    topic_terms: tuple[str, ...]
    supporting_ids: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "status": self.status,
            "demand_kind": self.demand_kind,
            "topic_terms": list(self.topic_terms),
            "supporting_ids": list(self.supporting_ids),
            "reason": self.reason,
        }


_DEMAND_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")
_SECOND_PERSON_RE = re.compile(r"\b(?:you|your|yours|yourself)\b", re.IGNORECASE)
_SELF_KNOWLEDGE_PATTERNS = (
    re.compile(r"\b(?:what|which)\s+(?:do|would|did)\s+you\s+(?:like|prefer|want|choose|pick|wear|use|keep|own)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+would\s+you\b.{0,80}\b(?:wear|wearing|choose|choosing|pick|picking)\b", re.IGNORECASE),
    re.compile(r"\b(?:do|did|have)\s+you\s+(?:ever\s+)?(?:actually\s+)?(?:like|prefer|want|care|cared|choose|pick|wear|use|keep|own|have|visit|meet|remember|experience|go|been|see|saw|read|buy|bought|try|tried)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is|was|are|were)\s+your\s+(?:favorite|favourite|preference|opinion|memory|experience|relationship|history)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+do\s+you\s+(?:feel|think)\s+about\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+do\s+you\s+remember\b", re.IGNORECASE),
)
_EXTERNAL_KNOWLEDGE_PATTERNS = (
    re.compile(r"\b(?:do\s+you\s+know|what\s+do\s+you\s+know)\s+(?:about\s+)?", re.IGNORECASE),
    re.compile(r"\b(?:what|who|where|when|why)\s+(?:is|are|was|were)\b", re.IGNORECASE),
    re.compile(r"\b(?:explain|define|tell\s+me\s+about)\b", re.IGNORECASE),
)
_DEMAND_NOISE = frozenset({
    "i", "me", "my", "mine", "you", "your", "yours", "yourself",
    "do", "did", "does", "would", "could", "should", "can", "have", "has", "had",
    "ever", "now", "then", "really", "just", "tell", "remember",
})


def _demand_clause(text: str) -> str:
    """Prefer the last explicit question over surrounding scene/dialogue prose."""
    clauses = [part.strip() for part in _DEMAND_SENTENCE_RE.findall(str(text or "")) if part.strip()]
    questions = [part for part in clauses if part.endswith("?")]
    if questions:
        return questions[-1]
    return clauses[-1] if clauses else str(text or "").strip()


_PREFERENCE_WORDS = frozenset({
    "like", "liked", "prefer", "preferred", "preference", "favorite", "favourite",
    "want", "wanted", "care", "cared", "choose", "chose", "chosen", "pick", "picked", "wear", "wearing",
})
_PREFERENCE_PREDICATES = frozenset({
    "like", "prefer", "want", "care", "choose", "pick", "favorite", "favourite",
})
_EXPERIENCE_WORDS = frozenset({
    "visit", "visited", "meet", "met", "go", "went", "been", "see", "saw",
    "read", "buy", "bought", "try", "tried", "experience", "experienced",
})
_MEMORY_WORDS = frozenset({"remember", "memory"})


def _demand_semantics(clause: str) -> tuple[str, tuple[str, ...], str]:
    terms = tuple(
        term for term in research_terms(clause, limit=16)
        if term not in _DEMAND_NOISE
    )
    term_set = set(terms)
    if term_set & _PREFERENCE_WORDS:
        kind = "preference"
    elif term_set & _MEMORY_WORDS:
        kind = "memory"
    elif term_set & _EXPERIENCE_WORDS:
        kind = "experience"
    else:
        kind = "history"
    semantic_words = _PREFERENCE_WORDS | _MEMORY_WORDS | _EXPERIENCE_WORDS
    topics = tuple(term for term in terms if term not in semantic_words)
    temporal = "prior" if re.search(r"\b(?:before|ever|previously|used to|in the past)\b", clause, re.IGNORECASE) else "unspecified"
    return kind, topics, temporal


def _item_terms(item: Mapping[str, object]) -> set[str]:
    terms: set[str] = set()
    for key in ("subject_norm", "predicate_norm", "object_norm", "topic_key", "text"):
        terms.update(research_terms(str(item.get(key) or ""), limit=128))
    return terms


def assess_retrieval_evidence(
    demand: RetrievalDemand,
    knowledge: Iterable[Mapping[str, object]],
) -> RetrievalEvidenceStatus | None:
    """Assess whether selected cognition answers a routed character demand.

    Relevance gets evidence into cognition; this check asks whether the selected
    propositions actually establish the requested relationship. It is transient
    and never writes a new character fact.
    """
    if demand.route != "character":
        return None

    topics = set(demand.topic_terms)
    related: list[Mapping[str, object]] = []
    established: list[Mapping[str, object]] = []
    for item in knowledge:
        item_terms = _item_terms(item)
        if topics and not (topics & item_terms):
            continue
        related.append(item)
        predicate = str(item.get("predicate_norm") or "").strip().lower()
        predicate_terms = set(research_terms(predicate, limit=16))
        subject_terms = set(research_terms(str(item.get("subject_norm") or ""), limit=16))
        demand_subject_terms = set(research_terms(demand.subject, limit=16))
        subject_matches = not demand_subject_terms or bool(subject_terms & demand_subject_terms)
        if demand.kind == "preference":
            if subject_matches and predicate_terms & _PREFERENCE_PREDICATES:
                established.append(item)
        elif demand.kind in {"experience", "memory", "history"}:
            # Event/memory propositions with a topical match establish that the
            # character has relevant prior experience/history, without claiming
            # any stronger preference or interpretation.
            kind = str(item.get("claim_kind") or "").upper()
            if kind in {"EVENT", "MEMORY"}:
                established.append(item)

    def ids(items: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
        values: list[str] = []
        for item in items:
            value = item.get("proposition_id") or item.get("semantic_event_id")
            if value is not None:
                values.append(str(value))
        return tuple(values[:8])

    if established:
        return RetrievalEvidenceStatus(
            demand.route, "established", demand.kind, demand.topic_terms,
            ids(established), "relation_supported",
        )
    if related:
        return RetrievalEvidenceStatus(
            demand.route, "partial", demand.kind, demand.topic_terms,
            ids(related), "topic_related_relation_unestablished",
        )
    return RetrievalEvidenceStatus(
        demand.route, "unestablished", demand.kind, demand.topic_terms,
        (), "no_related_character_evidence",
    )


def resolve_retrieval_demand(focus_text: str, *, character_id: str) -> RetrievalDemand:
    """Classify only high-confidence self-memory demands.

    Ambiguous language deliberately remains general so this layer cannot
    suppress ordinary world/corpus retrieval merely because a character is
    addressed in conversation.
    """
    raw = str(focus_text or "").strip()
    clause = _demand_clause(raw)
    if not clause:
        return RetrievalDemand("general", raw, "empty_focus")

    self_memory = bool(_SECOND_PERSON_RE.search(clause)) and any(
        pattern.search(clause) for pattern in _SELF_KNOWLEDGE_PATTERNS
    )
    if not self_memory:
        if any(pattern.search(clause) for pattern in _EXTERNAL_KNOWLEDGE_PATTERNS):
            return RetrievalDemand("general", raw, "external_knowledge_question")
        return RetrievalDemand("general", raw, "no_high_confidence_self_signal")

    terms = tuple(
        term for term in research_terms(clause, limit=16)
        if term not in _DEMAND_NOISE
    )
    compact = " ".join((str(character_id).strip(), *terms)).strip()
    kind, topic_terms, temporal_scope = _demand_semantics(clause)
    return RetrievalDemand(
        "character",
        compact or str(character_id).strip() or clause,
        "character_self_knowledge",
        kind=kind,
        subject=str(character_id).strip(),
        topic_terms=topic_terms,
        temporal_scope=temporal_scope,
    )


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
        include_fanwork: bool = True,
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
            SELECT (
                EXISTS (
                    SELECT 1 FROM aios.corpus_scope
                    WHERE access_class='public'
                )
                OR EXISTS (
                    SELECT 1
                    FROM aios.corpus_document_domain cdd
                    JOIN aios.character_knowledge_domain ckd
                      ON ckd.character_id=$1
                     AND ckd.enabled
                     AND ckd.knowledge_domain=cdd.knowledge_domain
                )
                OR EXISTS (
                    SELECT 1
                    FROM aios.corpus_source_profile csp
                    JOIN aios.corpus_scope scope_def
                      ON scope_def.scope_key=csp.scope_key
                     AND scope_def.access_class='domain'
                    JOIN aios.character_knowledge_domain ckd
                      ON ckd.character_id=$1
                     AND ckd.enabled
                     AND ckd.knowledge_domain=csp.knowledge_domain
                    WHERE csp.enabled
                )
                OR EXISTS (
                    SELECT 1 FROM aios.character_corpus_access
                    WHERE character_id=$1 AND allowed
                )
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
                   ts_rank_cd(cs.search_vector, q.query)
                   + CASE WHEN EXISTS (
                       SELECT 1
                       FROM aios.corpus_document_domain cdd
                       JOIN aios.character_knowledge_domain ckd
                         ON ckd.character_id=$1 AND ckd.enabled
                        AND ckd.knowledge_domain=cdd.knowledge_domain
                       WHERE cdd.document_id=cs.document_id
                   ) THEN 0.15 ELSE 0.0 END
                   + CASE WHEN EXISTS (
                       SELECT 1 FROM aios.corpus_document_facet facet
                       WHERE facet.document_id=cs.document_id
                         AND lower(facet.facet_value) = ANY($4::text[])
                   ) THEN 0.08 ELSE 0.0 END AS score,
                   CASE
                       WHEN length(cs.content) <= 1800 THEN cs.content
                       ELSE left(cs.content, 1800)
                   END AS excerpt,
                   array_agg(DISTINCT cds.scope_key ORDER BY cds.scope_key) AS scopes
            FROM aios.corpus_section cs
            JOIN aios.corpus_document cd ON cd.document_id=cs.document_id
            JOIN aios.corpus_document_scope cds ON cds.document_id=cs.document_id
            CROSS JOIN q
            WHERE cs.search_vector @@ q.query
              AND ($5::boolean OR NOT EXISTS (
                  SELECT 1 FROM aios.corpus_document_scope fan_scope
                  WHERE fan_scope.document_id=cs.document_id
                    AND fan_scope.scope_key ~ '(^|[.])fanwork([.]|$)'
              ))
              AND (
                  EXISTS (
                      SELECT 1
                      FROM aios.corpus_document_scope public_scope
                      JOIN aios.corpus_scope scope_def
                        ON scope_def.scope_key=public_scope.scope_key
                       AND scope_def.access_class='public'
                      WHERE public_scope.document_id=cs.document_id
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM aios.corpus_document_domain cdd
                      JOIN aios.character_knowledge_domain ckd
                        ON ckd.character_id=$1
                       AND ckd.enabled
                       AND ckd.knowledge_domain=cdd.knowledge_domain
                      WHERE cdd.document_id=cs.document_id
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM aios.corpus_document_collection domain_collection
                      JOIN aios.corpus_source_profile domain_profile
                        ON domain_profile.profile_id=domain_collection.profile_id
                       AND domain_profile.enabled
                      JOIN aios.corpus_scope domain_scope
                        ON domain_scope.scope_key=domain_profile.scope_key
                       AND domain_scope.access_class='domain'
                      JOIN aios.character_knowledge_domain ckd
                        ON ckd.character_id=$1
                       AND ckd.enabled
                       AND ckd.knowledge_domain=domain_profile.knowledge_domain
                      WHERE domain_collection.document_id=cs.document_id
                  )
                  OR EXISTS (
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
            list(terms),
            include_fanwork,
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
        include_fanwork: bool = True,
    ) -> CorpusResearchResult:
        return await self.searcher.search(
            instance_id=instance_id, query=query, limit=limit,
            include_fanwork=include_fanwork,
        )

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
                SELECT (
                    EXISTS (
                        SELECT 1
                        FROM aios.corpus_section cs
                        JOIN aios.corpus_document_scope ds ON ds.document_id=cs.document_id
                        JOIN aios.corpus_scope scope_def
                          ON scope_def.scope_key=ds.scope_key
                         AND scope_def.access_class='public'
                        WHERE cs.section_id=$1
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM aios.corpus_section cs
                        JOIN aios.corpus_document_domain cdd
                          ON cdd.document_id=cs.document_id
                        JOIN aios.character_knowledge_domain ckd
                          ON ckd.character_id=$2
                         AND ckd.enabled
                         AND ckd.knowledge_domain=cdd.knowledge_domain
                        WHERE cs.section_id=$1
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM aios.corpus_section cs
                        JOIN aios.corpus_document_collection domain_collection
                          ON domain_collection.document_id=cs.document_id
                        JOIN aios.corpus_source_profile domain_profile
                          ON domain_profile.profile_id=domain_collection.profile_id
                         AND domain_profile.enabled
                        JOIN aios.corpus_scope domain_scope
                          ON domain_scope.scope_key=domain_profile.scope_key
                         AND domain_scope.access_class='domain'
                        JOIN aios.character_knowledge_domain ckd
                          ON ckd.character_id=$2
                         AND ckd.enabled
                         AND ckd.knowledge_domain=domain_profile.knowledge_domain
                        WHERE cs.section_id=$1
                    )
                    OR EXISTS (
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
                    )
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


@dataclass(frozen=True)
class CorpusLearningDecision:
    section_id: UUID
    eligible: bool
    score: float
    threshold: float
    exposure_count: int
    reinforcement: float
    reason: str


class CorpusLearningPolicy:
    """Deterministic exposure-to-learning policy.

    This score estimates whether the character attended to material enough for
    durable acquisition. It is intentionally separate from proposition truth or
    confidence, which remain the responsibility of the epistemic pipeline.
    """

    def __init__(self, db: Database, *, threshold: float = 0.72):
        self.db = db
        self.threshold = max(0.0, min(float(threshold), 1.0))

    @staticmethod
    def _profile_map(value: object) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return dict(value or {})

    @staticmethod
    def _scope_value(mapping: Mapping[str, object], scopes: Sequence[str], default: float) -> float:
        values: list[float] = []
        for scope in scopes:
            parts = scope.split(".")
            candidates = [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
            for candidate in reversed(candidates):
                if candidate in mapping:
                    try:
                        values.append(float(mapping[candidate]))
                    except (TypeError, ValueError):
                        pass
                    break
        return max(values, default=default)

    async def evaluate(
        self,
        *,
        instance_id: UUID,
        research_id: UUID,
        section_id: UUID,
    ) -> CorpusLearningDecision:
        row = await self.db.fetchrow(
            """
            SELECT cre.character_id, cce.rank, cce.score AS retrieval_score,
                   cce.exposed_at,
                   COALESCE(array_agg(DISTINCT cds.scope_key)
                            FILTER (WHERE cds.scope_key IS NOT NULL), '{}') AS scopes,
                   (
                       SELECT count(*)
                       FROM aios.character_corpus_exposure prior
                       JOIN aios.character_research_event prior_event
                         ON prior_event.research_id=prior.research_id
                       WHERE prior_event.character_id=cre.character_id
                         AND prior.section_id=cce.section_id
                         AND prior.exposed_at <= cce.exposed_at
                   ) AS exposure_count,
                   COALESCE((
                       SELECT sum(r.strength)
                       FROM aios.character_corpus_reinforcement r
                       WHERE r.character_id=cre.character_id
                         AND r.section_id=cce.section_id
                         AND r.created_at <= cce.exposed_at
                   ), 0.0) AS reinforcement
            FROM aios.character_corpus_exposure cce
            JOIN aios.character_research_event cre ON cre.research_id=cce.research_id
            JOIN aios.corpus_section cs ON cs.section_id=cce.section_id
            LEFT JOIN aios.corpus_document_scope cds ON cds.document_id=cs.document_id
            WHERE cce.research_id=$1 AND cce.section_id=$2
              AND cre.instance_id=$3
            GROUP BY cre.character_id, cce.rank, cce.score, cce.exposed_at, cce.section_id
            """,
            research_id,
            section_id,
            instance_id,
        )
        if not row:
            raise ValueError("corpus exposure not found for research event and instance")

        profile = await get_profile(self.db, character_id=str(row["character_id"]))
        curiosity = max(0.0, min(float(profile.get("curiosity", 0.5)), 1.0))
        retention = max(0.0, min(float(profile.get("retention", 0.7)), 1.0))
        novelty = max(0.0, min(float(profile.get("novelty_seeking", 0.5)), 1.0))
        interest_map = self._profile_map(profile.get("topic_interest"))
        expertise_map = self._profile_map(profile.get("domain_expertise"))
        scopes = tuple(row["scopes"] or ())
        interest = self._scope_value(interest_map, scopes, 0.5)
        expertise = self._scope_value(expertise_map, scopes, 0.5)

        # PostgreSQL FTS rank is not normalized. Treat any useful returned hit
        # as baseline relevance and let rank/repetition/profile determine learning.
        retrieval = max(0.0, min(float(row["retrieval_score"] or 0.0) * 4.0, 1.0))
        rank_factor = 1.0 / max(1, int(row["rank"] or 1))
        exposure_count = int(row["exposure_count"] or 1)
        repetition = min(1.0, exposure_count / 3.0)
        reinforcement = min(1.0, float(row["reinforcement"] or 0.0) / 2.0)

        score = (
            0.20 * retrieval
            + 0.10 * rank_factor
            + 0.15 * repetition
            + 0.18 * reinforcement
            + 0.12 * curiosity
            + 0.12 * retention
            + 0.06 * max(0.0, min(interest, 1.0))
            + 0.05 * max(0.0, min(expertise, 1.0))
            + 0.02 * novelty
        )
        score = max(0.0, min(score, 1.0))
        eligible = score >= self.threshold
        reason = "learning_threshold_met" if eligible else "reference_only"
        return CorpusLearningDecision(
            section_id=section_id,
            eligible=eligible,
            score=score,
            threshold=self.threshold,
            exposure_count=exposure_count,
            reinforcement=reinforcement,
            reason=reason,
        )


class CorpusLearningService:
    """Evaluate exposed corpus evidence and selectively cross it into /char."""

    def __init__(self, db: Database):
        self.db = db
        self.policy = CorpusLearningPolicy(db)
        self.research = CharacterResearchService(db)

    async def evaluate_and_acquire(
        self,
        *,
        instance_id: UUID,
        research_id: UUID,
        section_ids: Sequence[UUID],
    ) -> dict:
        decisions: list[CorpusLearningDecision] = []
        eligible: list[UUID] = []
        for section_id in section_ids:
            decision = await self.policy.evaluate(
                instance_id=instance_id,
                research_id=research_id,
                section_id=section_id,
            )
            decisions.append(decision)
            status = "eligible" if decision.eligible else "reference"
            await self.db.execute(
                """
                UPDATE aios.character_corpus_exposure
                SET acquisition_status=$4,
                    acquisition_score=$5,
                    acquisition_reason=$6,
                    evaluated_at=now()
                WHERE research_id=$1 AND section_id=$2
                  AND EXISTS (
                      SELECT 1 FROM aios.character_research_event cre
                      WHERE cre.research_id=$1 AND cre.instance_id=$3
                  )
                """,
                research_id,
                section_id,
                instance_id,
                status,
                decision.score,
                decision.reason,
            )
            if decision.eligible:
                eligible.append(section_id)

        consumption_by_section: dict[UUID, UUID] = {}
        if eligible:
            result = await self.research.acquire(
                instance_id=instance_id,
                section_ids=eligible,
                mode="research",
            )
            for section_id, consumption_id in zip(eligible, result["consumption_ids"]):
                consumption_by_section[section_id] = consumption_id
                await self.db.execute(
                    """
                    UPDATE aios.character_corpus_exposure
                    SET acquisition_status='acquired',
                        consumption_id=$3,
                        evaluated_at=now()
                    WHERE research_id=$1 AND section_id=$2
                    """,
                    research_id,
                    section_id,
                    consumption_id,
                )

        return {
            "instance_id": instance_id,
            "research_id": research_id,
            "decisions": [
                {
                    "section_id": decision.section_id,
                    "eligible": decision.eligible,
                    "score": decision.score,
                    "threshold": decision.threshold,
                    "exposure_count": decision.exposure_count,
                    "reinforcement": decision.reinforcement,
                    "reason": decision.reason,
                    "consumption_id": consumption_by_section.get(decision.section_id),
                }
                for decision in decisions
            ],
            "acquired_count": len(consumption_by_section),
        }


class CorpusReinforcementService:
    """Record deterministic attention signals for previously exposed sections."""

    def __init__(self, db: Database):
        self.db = db

    async def reinforce_from_focus(
        self,
        *,
        instance_id: UUID,
        focus_text: str,
        current_research_id: UUID | None = None,
        lookback: int = 24,
        signal_kind: str = "focus_overlap",
    ) -> list[dict]:
        focus_terms = set(research_terms(focus_text, limit=32))
        if not focus_terms:
            return []

        instance = await self.db.fetchrow(
            "SELECT character_id FROM aios.character_instance WHERE instance_id=$1",
            instance_id,
        )
        if not instance:
            raise ValueError(f"unknown character instance {instance_id}")
        character_id = str(instance["character_id"])

        rows = await self.db.fetch(
            """
            SELECT cce.section_id, cre.research_id, cs.heading, cs.content
            FROM aios.character_corpus_exposure cce
            JOIN aios.character_research_event cre ON cre.research_id=cce.research_id
            JOIN aios.corpus_section cs ON cs.section_id=cce.section_id
            WHERE cre.character_id=$1
              AND ($2::uuid IS NULL OR cre.research_id <> $2)
              AND cce.acquisition_status <> 'acquired'
            ORDER BY cce.exposed_at DESC
            LIMIT $3
            """,
            character_id,
            current_research_id,
            max(1, min(int(lookback), 100)),
        )

        reinforced: list[dict] = []
        seen_sections: set[UUID] = set()
        for row in rows:
            section_id = row["section_id"]
            if section_id in seen_sections:
                continue
            seen_sections.add(section_id)
            section_terms = set(
                research_terms(
                    f"{row['heading'] or ''} {row['content'] or ''}",
                    limit=256,
                )
            )
            overlap = focus_terms & section_terms
            if not overlap:
                continue
            strength = min(1.0, len(overlap) / max(2, min(len(focus_terms), 6)))
            if strength < 0.25:
                continue
            await self.db.execute(
                """
                INSERT INTO aios.character_corpus_reinforcement (
                    character_id, instance_id, section_id, research_id,
                    signal_kind, strength, meta
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                ON CONFLICT DO NOTHING
                """,
                character_id,
                instance_id,
                section_id,
                current_research_id,
                signal_kind,
                strength,
                json.dumps({"overlap_terms": sorted(overlap)}),
            )
            reinforced.append({
                "section_id": section_id,
                "strength": strength,
                "overlap_terms": sorted(overlap),
            })
        return reinforced


class SemanticKnowledgeCoverageService:
    """Deterministic proposition-aware character knowledge coverage."""

    @staticmethod
    def _field_terms(item: Mapping[str, object]) -> dict[str, set[str]]:
        return {
            "subject": set(research_terms(str(item.get("subject_norm") or ""), limit=32)),
            "predicate": set(research_terms(str(item.get("predicate_norm") or ""), limit=32)),
            "object": set(research_terms(str(item.get("object_norm") or ""), limit=64)),
            "topic": set(research_terms(str(item.get("topic_key") or ""), limit=32)),
            "text": set(research_terms(str(item.get("text") or ""), limit=128)),
        }

    def resolve(
        self,
        focus_text: str,
        *,
        knowledge: Iterable[Mapping[str, object]],
        minimum_terms: int = 2,
        threshold: float = 0.60,
    ) -> KnowledgeDemand:
        terms = research_terms(focus_text)
        if len(terms) < max(1, int(minimum_terms)):
            return KnowledgeDemand(False, terms, (), 1.0, "insufficient_terms")

        best: dict[str, float] = {term: 0.0 for term in terms}
        for item in knowledge:
            fields = self._field_terms(item)
            confidence = item.get("effective_confidence", item.get("confidence", 0.5))
            try:
                confidence_factor = max(0.25, min(float(confidence), 1.0))
            except (TypeError, ValueError):
                confidence_factor = 0.5
            for term in terms:
                weight = 0.0
                if term in fields["subject"] or term in fields["object"]:
                    weight = 1.0
                elif term in fields["predicate"] or term in fields["topic"]:
                    weight = 0.85
                elif term in fields["text"]:
                    weight = 0.45
                best[term] = max(best[term], weight * confidence_factor)

        coverage = sum(best.values()) / len(terms)
        missing = tuple(term for term in terms if best[term] < 0.45)
        needed = bool(missing) and coverage < max(0.0, min(float(threshold), 1.0))
        return KnowledgeDemand(
            needed=needed,
            terms=terms,
            missing_terms=missing,
            coverage=coverage,
            reason="semantic_knowledge_gap" if needed else "semantic_coverage_sufficient",
        )


class SemanticCorpusReinforcementService(CorpusReinforcementService):
    """Reinforce old corpus exposure from structured current propositions."""

    @staticmethod
    def _knowledge_terms(knowledge: Iterable[Mapping[str, object]]) -> set[str]:
        terms: set[str] = set()
        for item in knowledge:
            for key in ("subject_norm", "predicate_norm", "object_norm", "topic_key"):
                terms.update(research_terms(str(item.get(key) or ""), limit=64))
        return terms

    async def reinforce_from_knowledge(
        self,
        *,
        instance_id: UUID,
        knowledge: Iterable[Mapping[str, object]],
        current_research_id: UUID | None = None,
        lookback: int = 24,
    ) -> list[dict]:
        semantic_terms = self._knowledge_terms(knowledge)
        if not semantic_terms:
            return []
        return await self.reinforce_from_focus(
            instance_id=instance_id,
            focus_text=" ".join(sorted(semantic_terms)),
            current_research_id=current_research_id,
            lookback=lookback,
            signal_kind="semantic_overlap",
        )
