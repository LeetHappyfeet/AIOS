"""Pure, no-provider tests for shadow corpus structural gap discovery."""
from aios_app.knowledge_enrichment.gaps import (
    PAIR_THRESHOLD, candidate_pairs, revision_key,
)


def row(domain, section, document, topic, similarity=0.91, revision="r1"):
    return {
        "domain_key": domain,
        "section_id": section,
        "document_id": document,
        "topic_id": topic,
        "similarity": similarity,
        "source_fingerprint": revision,
        "reference_revision": 1,
    }


def test_pair_requires_independent_documents():
    rows = [
        row("fiction.example", "s1", "d1", "t1"),
        row("fiction.example", "s1", "d1", "t2"),
        row("fiction.example", "s2", "d1", "t1"),
        row("fiction.example", "s2", "d1", "t2"),
    ]
    assert candidate_pairs(rows) == []
    rows.extend([
        row("fiction.example", "s3", "d2", "t1"),
        row("fiction.example", "s3", "d2", "t2"),
    ])
    result = candidate_pairs(rows)
    assert len(result) == 1
    assert result[0]["documents"] == ["d1", "d2"]
    assert result[0]["signals"]["matching_sections"] == 3


def test_no_cross_domain_pair_or_low_similarity():
    rows = [
        row("fiction.one", "s1", "d1", "t1"),
        row("fiction.one", "s1", "d1", "t2"),
        row("fiction.two", "s2", "d2", "t1"),
        row("fiction.two", "s2", "d2", "t2"),
        row("fiction.one", "s3", "d3", "t1"),
        row("fiction.one", "s3", "d3", "t2", PAIR_THRESHOLD - 0.01),
    ]
    assert candidate_pairs(rows) == []


def test_revision_receipt_stable_and_changes_with_source():
    a = [("s1", "fingerprint:1"), ("s2", "fingerprint:1")]
    b = list(reversed(a))
    assert revision_key(a) == revision_key(b)
    assert revision_key(a) != revision_key([("s1", "fingerprint:2"), a[1]])


def test_no_duplicate_topics_or_sections():
    rows = [
        row("fiction.example", "s1", "d1", "t1"),
        row("fiction.example", "s1", "d1", "t1"),
        row("fiction.example", "s1", "d1", "t2"),
        row("fiction.example", "s2", "d2", "t1"),
        row("fiction.example", "s2", "d2", "t2"),
    ]
    pairs = candidate_pairs(rows)
    assert len(pairs) == 1
    assert pairs[0]["signals"]["matching_sections"] == 2
