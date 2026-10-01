# Semantic integrity deployment notes (2026-10-01)

Apply migrations/current/20261001_claim_semantic_integrity.sql before starting
workers on this revision. This pass deliberately fails closed on claims without
a valid occurrence-scoped integrity receipt. Legacy observations require a
separate controlled backfill and reprojection; do not set all historical claims
to 'valid' in SQL.

The experiment 7d1fd994-5ce1-4336-9cdb-54be0c98c388 remains historical
comparison data. Its v1/v2 decisions were evaluated before source-integrity
admission and must not be used as ground-truth labels.

Known limitations: ambiguous third-person anaphora is retained for later
resolution; the bounded deterministic repair does not yet call the inference
broker. Running migration and integration tests requires the AIOS database and
installed English spaCy model. This change has not been validated against those
runtime dependencies through the repository connector.
