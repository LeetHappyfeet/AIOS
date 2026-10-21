BEGIN;

-- Source snapshots contain mentions, stable anchors and qualified relations.
-- Deliberately retain tombstones after source deletion until RDF is cleared.
CREATE TABLE aios.entity_memory_projection (
    section_id uuid PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 1,
    built_revision bigint NOT NULL DEFAULT 0,
    rdf_revision bigint NOT NULL DEFAULT 0,
    policy_version text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    identity_names text[] NOT NULL DEFAULT '{}',
    dataset text CHECK (dataset IN ('char','world')),
    projected_dataset text CHECK (projected_dataset IN ('char','world')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    retry_after timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entity_memory_identity_names ON aios.entity_memory_projection USING gin(identity_names);
CREATE INDEX entity_memory_build_pending ON aios.entity_memory_projection(retry_after,section_id)
    WHERE built_revision < revision;
CREATE INDEX entity_memory_rdf_pending ON aios.entity_memory_projection(updated_at,section_id)
    WHERE rdf_revision < built_revision;

CREATE FUNCTION aios.dirty_entity_memory_section(p_section uuid) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO aios.entity_memory_projection(section_id) VALUES(p_section)
    ON CONFLICT(section_id) DO UPDATE SET revision=aios.entity_memory_projection.revision+1,
        updated_at=now(),retry_after=now();
$$;

CREATE FUNCTION aios.entity_memory_source_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        PERFORM aios.dirty_entity_memory_section(OLD.section_id);
        RETURN OLD;
    END IF;
    PERFORM aios.dirty_entity_memory_section(NEW.section_id);
    RETURN NEW;
END;
$$;
CREATE TRIGGER entity_memory_linguistic_dirty
AFTER INSERT OR UPDATE OR DELETE ON aios.linguistic_projection
FOR EACH ROW EXECUTE FUNCTION aios.entity_memory_source_dirty();

CREATE FUNCTION aios.entity_memory_context_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p_section uuid;
BEGIN
    SELECT es.section_id INTO p_section FROM aios.claim_candidate cc
    JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
    WHERE cc.claim_id=COALESCE(NEW.claim_id,OLD.claim_id);
    IF p_section IS NOT NULL THEN PERFORM aios.dirty_entity_memory_section(p_section); END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER entity_memory_context_dirty AFTER INSERT OR UPDATE OR DELETE
ON aios.claim_context_resolution FOR EACH ROW EXECUTE FUNCTION aios.entity_memory_context_dirty();

CREATE FUNCTION aios.entity_memory_event_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p_section uuid;
BEGIN
    IF NEW.superseded_at IS DISTINCT FROM OLD.superseded_at THEN
        FOR p_section IN SELECT ds.section_id FROM aios.document_section ds
            JOIN aios.dag_node n ON n.node_id=ds.node_id WHERE n.event_id=NEW.event_id
        LOOP PERFORM aios.dirty_entity_memory_section(p_section); END LOOP;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER entity_memory_event_dirty AFTER UPDATE OF superseded_at ON aios.ingest_event
FOR EACH ROW EXECUTE FUNCTION aios.entity_memory_event_dirty();

-- Indexed invalidation touches only sources that looked up the affected names.
CREATE FUNCTION aios.entity_memory_identity_dirty() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE names text[];
BEGIN
    names := ARRAY[
        lower(btrim(to_jsonb(NEW)->>'character_id')),lower(btrim(to_jsonb(OLD)->>'character_id')),
        lower(btrim(to_jsonb(NEW)->>'display_name')),lower(btrim(to_jsonb(OLD)->>'display_name')),
        lower(btrim(to_jsonb(NEW)->>'canonical_name')),lower(btrim(to_jsonb(OLD)->>'canonical_name')),
        lower(btrim(to_jsonb(NEW)->>'alias')),lower(btrim(to_jsonb(OLD)->>'alias'))
    ];
    UPDATE aios.entity_memory_projection SET revision=revision+1,retry_after=now(),updated_at=now()
    WHERE identity_names && names;
    RETURN NULL;
END;
$$;
CREATE TRIGGER entity_memory_identity_dirty AFTER INSERT OR UPDATE OR DELETE
ON aios.character_identity FOR EACH ROW EXECUTE FUNCTION aios.entity_memory_identity_dirty();
CREATE TRIGGER entity_memory_alias_dirty AFTER INSERT OR UPDATE OR DELETE
ON aios.character_alias FOR EACH ROW EXECUTE FUNCTION aios.entity_memory_identity_dirty();

INSERT INTO aios.entity_memory_projection(section_id)
SELECT section_id FROM aios.linguistic_projection ON CONFLICT DO NOTHING;
COMMIT;
