BEGIN;
-- Independent queue/cache: no ingest events, claims, grants or acquisitions.
-- No FK: deletion leaves a tombstone until the old RDF graph is withdrawn.
CREATE TABLE aios.corpus_reference_projection (
    section_id uuid PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 1,
    built_revision bigint NOT NULL DEFAULT 0,
    rdf_revision bigint NOT NULL DEFAULT 0,
    parse_sha256 text,
    doc_json jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    policy_version text,
    retry_after timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);
CREATE INDEX corpus_reference_build_pending ON aios.corpus_reference_projection(retry_after,section_id)
    WHERE built_revision<revision;
CREATE INDEX corpus_reference_rdf_pending ON aios.corpus_reference_projection(updated_at,section_id)
    WHERE rdf_revision<built_revision;

CREATE FUNCTION aios.dirty_corpus_reference(p_section uuid) RETURNS void LANGUAGE sql AS $$
    INSERT INTO aios.corpus_reference_projection(section_id) VALUES(p_section)
    ON CONFLICT(section_id) DO UPDATE SET revision=aios.corpus_reference_projection.revision+1,
        retry_after=now(),updated_at=now();
$$;
CREATE FUNCTION aios.dirty_corpus_reference_document(p_document uuid) RETURNS void LANGUAGE sql AS $$
    INSERT INTO aios.corpus_reference_projection(section_id)
    SELECT section_id FROM aios.corpus_section WHERE document_id=p_document
    ON CONFLICT(section_id) DO UPDATE SET revision=aios.corpus_reference_projection.revision+1,
        retry_after=now(),updated_at=now();
$$;
CREATE FUNCTION aios.corpus_reference_section_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.dirty_corpus_reference(COALESCE(NEW.section_id,OLD.section_id));
    RETURN NULL;
END;
$$;
CREATE TRIGGER corpus_reference_section_dirty AFTER INSERT OR UPDATE OR DELETE ON aios.corpus_section
    FOR EACH ROW EXECUTE FUNCTION aios.corpus_reference_section_dirty();

CREATE FUNCTION aios.corpus_reference_document_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM aios.dirty_corpus_reference_document(NEW.document_id);
    RETURN NULL;
END;
$$;
CREATE TRIGGER corpus_reference_document_dirty AFTER UPDATE OF title,source_id,source_uri,document_kind,
    epistemic_namespace,identity_binding,meta,language ON aios.corpus_document
    FOR EACH ROW EXECUTE FUNCTION aios.corpus_reference_document_dirty();

CREATE FUNCTION aios.corpus_reference_membership_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN PERFORM aios.dirty_corpus_reference_document(OLD.document_id); END IF;
    IF TG_OP<>'DELETE' THEN PERFORM aios.dirty_corpus_reference_document(NEW.document_id); END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER corpus_reference_domain_dirty AFTER INSERT OR UPDATE OR DELETE ON aios.corpus_document_domain
    FOR EACH ROW EXECUTE FUNCTION aios.corpus_reference_membership_dirty();
CREATE TRIGGER corpus_reference_scope_dirty AFTER INSERT OR UPDATE OR DELETE ON aios.corpus_document_scope
    FOR EACH ROW EXECUTE FUNCTION aios.corpus_reference_membership_dirty();
CREATE TRIGGER corpus_reference_facet_dirty AFTER INSERT OR UPDATE OR DELETE ON aios.corpus_document_facet
    FOR EACH ROW EXECUTE FUNCTION aios.corpus_reference_membership_dirty();

CREATE FUNCTION aios.corpus_reference_registry_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d uuid;
BEGIN
    FOR d IN SELECT document_id FROM aios.corpus_document_domain
        WHERE knowledge_domain=OLD.domain_key OR knowledge_domain=NEW.domain_key
    LOOP PERFORM aios.dirty_corpus_reference_document(d); END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER corpus_reference_registry_dirty AFTER INSERT OR UPDATE OR DELETE ON aios.knowledge_domain
    FOR EACH ROW EXECUTE FUNCTION aios.corpus_reference_registry_dirty();

INSERT INTO aios.corpus_reference_projection(section_id) SELECT section_id FROM aios.corpus_section;
COMMENT ON TABLE aios.corpus_reference_projection IS
'Regenerable corpus metadata, provisional entities and attributed relation evidence. RDF presence grants no access, world truth or character knowledge.';
COMMENT ON TABLE aios.corpus_section IS
'Cold reference text. Advisory corpus RDF may describe it; semantic ingestion and character acquisition still require explicit consumption.';
COMMIT;
