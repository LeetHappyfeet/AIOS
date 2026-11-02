BEGIN;

-- An input revision is independent of normalization/integrity output. This
-- lets discovery skip a blocked claim with one primary-key lookup.
CREATE TABLE aios.claim_normalization_state (
    claim_id uuid PRIMARY KEY REFERENCES aios.claim_candidate(claim_id) ON DELETE CASCADE,
    input_revision bigint NOT NULL DEFAULT 1 CHECK (input_revision > 0),
    attempted_revision bigint,
    outcome text CHECK (outcome IN ('blocked','normalized')),
    normalizer_version text,
    validator_version text,
    repair_version text,
    integrity_revision_key text,
    integrity_status text,
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    attempted_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- Existing incomplete claims get one attempt under the new policy. Seeding
-- all rows also ensures concurrent source edits can always bump a revision.
INSERT INTO aios.claim_normalization_state(claim_id)
SELECT claim_id FROM aios.claim_candidate;

CREATE FUNCTION aios.invalidate_normalization_input() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_data jsonb;
    new_data jsonb;
    ignored text[] := ARRAY['created_at','updated_at','resolved_at','checked_at','built_at','projected_at',
        'claims_extracted_at','processed_at','dag_processed_at','section_processed_at','claims_processed_at','rdf_processed_at'];
BEGIN
    IF TG_OP <> 'INSERT' THEN old_data := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN new_data := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' AND (old_data - ignored) IS NOT DISTINCT FROM (new_data - ignored) THEN
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'claim_candidate' THEN
        IF TG_OP = 'INSERT' THEN
            INSERT INTO aios.claim_normalization_state(claim_id)
            VALUES (NEW.claim_id) ON CONFLICT DO NOTHING;
        ELSE
            UPDATE aios.claim_normalization_state SET input_revision=input_revision+1,updated_at=now()
            WHERE claim_id IN ((old_data->>'claim_id')::uuid,(new_data->>'claim_id')::uuid);
        END IF;
    ELSIF TG_TABLE_NAME IN ('claim_semantic_frame','claim_semantic_frame_projection','claim_context_resolution') THEN
        UPDATE aios.claim_normalization_state SET input_revision=input_revision+1,updated_at=now()
        WHERE claim_id IN ((old_data->>'claim_id')::uuid,(new_data->>'claim_id')::uuid);
    ELSIF TG_TABLE_NAME = 'extracted_sentence' THEN
        UPDATE aios.claim_normalization_state ns SET input_revision=input_revision+1,updated_at=now()
        WHERE ns.claim_id IN (SELECT cc.claim_id FROM aios.claim_candidate cc
            WHERE cc.sentence_id IN ((old_data->>'sentence_id')::uuid,(new_data->>'sentence_id')::uuid));
    ELSIF TG_TABLE_NAME = 'document_section' THEN
        UPDATE aios.claim_normalization_state ns SET input_revision=input_revision+1,updated_at=now()
        WHERE ns.claim_id IN (SELECT cc.claim_id FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            WHERE es.section_id IN ((old_data->>'section_id')::uuid,(new_data->>'section_id')::uuid));
    ELSIF TG_TABLE_NAME = 'dag_node' THEN
        UPDATE aios.claim_normalization_state ns SET input_revision=input_revision+1,updated_at=now()
        WHERE ns.claim_id IN (SELECT cc.claim_id FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            WHERE ds.node_id IN ((old_data->>'node_id')::uuid,(new_data->>'node_id')::uuid));
    ELSIF TG_TABLE_NAME = 'ingest_event' THEN
        UPDATE aios.claim_normalization_state ns SET input_revision=input_revision+1,updated_at=now()
        WHERE ns.claim_id IN (SELECT cc.claim_id FROM aios.claim_candidate cc
            JOIN aios.extracted_sentence es ON es.sentence_id=cc.sentence_id
            JOIN aios.document_section ds ON ds.section_id=es.section_id
            JOIN aios.dag_node dn ON dn.node_id=ds.node_id
            WHERE dn.event_id IN ((old_data->>'event_id')::bigint,(new_data->>'event_id')::bigint));
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER normalization_claim_input AFTER INSERT OR UPDATE ON aios.claim_candidate
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
CREATE TRIGGER normalization_frame_input AFTER INSERT OR UPDATE OR DELETE ON aios.claim_semantic_frame
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
CREATE TRIGGER normalization_frame_projection_input AFTER INSERT OR UPDATE OR DELETE ON aios.claim_semantic_frame_projection
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
CREATE TRIGGER normalization_context_input AFTER INSERT OR UPDATE OR DELETE ON aios.claim_context_resolution
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
CREATE TRIGGER normalization_sentence_input AFTER UPDATE ON aios.extracted_sentence
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
CREATE TRIGGER normalization_section_input AFTER UPDATE ON aios.document_section
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
CREATE TRIGGER normalization_node_input AFTER UPDATE ON aios.dag_node
FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();
-- Processing flags are outputs, not new source evidence.
CREATE TRIGGER normalization_event_input AFTER UPDATE OF source,source_id,source_kind,superseded_at
ON aios.ingest_event FOR EACH ROW EXECUTE FUNCTION aios.invalidate_normalization_input();

COMMENT ON TABLE aios.claim_normalization_state IS
'Normalization outcome for an input revision. Blocked is not publication, belief or evidence. Source/frame/context mutations invalidate the revision; validator/normalizer/repair version changes permit another attempt.';
COMMIT;
