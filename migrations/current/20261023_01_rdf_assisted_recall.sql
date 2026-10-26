BEGIN;
ALTER TABLE aios.entity_memory_projection ADD COLUMN search_terms text[] NOT NULL DEFAULT '{}';
ALTER TABLE aios.entity_memory_projection ADD COLUMN owner_character_id text;
ALTER TABLE aios.entity_memory_projection ADD COLUMN source_timeline_id uuid;
CREATE INDEX entity_memory_search_terms ON aios.entity_memory_projection USING gin(search_terms);
CREATE INDEX entity_memory_owner_timeline ON aios.entity_memory_projection(owner_character_id,source_timeline_id);

CREATE FUNCTION aios.entity_memory_search_coordinates() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.owner_character_id := NEW.payload->>'owner';
    NEW.source_timeline_id := NULLIF(NEW.payload->>'timeline_id','')::uuid;
    SELECT COALESCE(array_agg(DISTINCT term ORDER BY term),ARRAY[]::text[]) INTO NEW.search_terms
    FROM (
        SELECT lower(word) AS term
        FROM jsonb_array_elements(COALESCE(NEW.payload->'mentions','[]'::jsonb)) m,
             LATERAL regexp_split_to_table(COALESCE(m->>'head','') || ' ' || COALESCE(m->>'text',''),'[^[:alnum:]_-]+') word
        WHERE m->>'entity_id' IS NOT NULL AND length(word)>=3
    ) terms;
    RETURN NEW;
END;
$$;
CREATE TRIGGER entity_memory_search_coordinates BEFORE INSERT OR UPDATE OF payload
ON aios.entity_memory_projection FOR EACH ROW EXECUTE FUNCTION aios.entity_memory_search_coordinates();

-- Rebuild from the existing parse to supply hashes and section-relative sentence spans.
UPDATE aios.entity_memory_projection SET revision=revision+1,retry_after=now(),updated_at=now();

CREATE TABLE aios.entity_recall_trace (
    trace_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    source_head_node_id uuid,
    scope_key text NOT NULL,
    query_hash text NOT NULL,
    status text NOT NULL,
    refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb,
    consumed_by text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now()+interval '15 seconds'
);
CREATE INDEX entity_recall_ready ON aios.entity_recall_trace(instance_id,source_head_node_id,scope_key,query_hash,created_at DESC);
COMMENT ON TABLE aios.entity_recall_trace IS
'RDF discovery and selected source references, revalidated before use. Not belief, acquisition or goal completion.';
COMMIT;
