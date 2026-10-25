-- Advisory continuation survives optional retrieval failures independently of inference.
ALTER TABLE aios.character_goal_plan
 ADD COLUMN continuation jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(continuation)='object'),
 ADD COLUMN continuation_check_at timestamptz DEFAULT now();
CREATE INDEX idx_goal_plan_continuation_due
 ON aios.character_goal_plan(continuation_check_at)
 WHERE status='waiting_information';

CREATE INDEX idx_cognitive_requirement_receipt
 ON aios.character_cognitive_operation(instance_id,(input->>'requirement_id'),updated_at DESC)
 WHERE operation_type IN ('inquiry.resolve','research.advance','research.study');

-- Catalog changes invalidate retry decisions; this epoch never authorizes a source.
CREATE TABLE aios.corpus_catalog_epoch (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO aios.corpus_catalog_epoch DEFAULT VALUES;
CREATE FUNCTION aios.touch_corpus_catalog_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE aios.corpus_catalog_epoch SET changed_at=clock_timestamp() WHERE singleton;
 RETURN NULL;
END $$;
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['corpus_source_profile','corpus_document_domain',
   'corpus_document_scope','corpus_document_collection','corpus_scope'] LOOP
  IF to_regclass('aios.'||tab) IS NOT NULL THEN
   EXECUTE format('CREATE TRIGGER corpus_catalog_changed AFTER INSERT OR UPDATE OR DELETE ON aios.%I FOR EACH STATEMENT EXECUTE FUNCTION aios.touch_corpus_catalog_epoch()',tab);
  END IF;
 END LOOP;
END $$;

CREATE TABLE aios.character_corpus_lookup_trace (
 instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
 source_node_key text NOT NULL,
 query_hash text NOT NULL,
 diagnostics jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(instance_id,source_node_key,query_hash)
);
