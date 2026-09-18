-- Incremental semantic-topology RDF projection
-- 2026-09-18
--
-- PostgreSQL remains authoritative. Record topology mutations durably so the
-- normal Fuseki projection path can publish deltas instead of rebuilding an
-- entire scope. Full scope projection remains the bootstrap/repair path.

BEGIN;

ALTER TABLE aios.semantic_scope_projection_state
    ADD COLUMN IF NOT EXISTS rdf_change_cursor bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rdf_delta_ready boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS aios.semantic_rdf_change (
    change_id bigserial PRIMARY KEY,
    scope_key text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('node','edge','anchor')),
    object_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation IN ('upsert','delete')),
    old_state jsonb,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_rdf_change_scope_cursor
    ON aios.semantic_rdf_change (scope_key, change_id);
CREATE INDEX IF NOT EXISTS idx_semantic_rdf_change_object
    ON aios.semantic_rdf_change (scope_key, object_kind, object_id, change_id DESC);

COMMENT ON TABLE aios.semantic_rdf_change IS
    'Durable mutation outbox for incremental semantic-topology RDF replication. Delete rows retain the old RDF identity coordinates needed to retract direct relationship triples.';

CREATE OR REPLACE FUNCTION aios.record_semantic_topology_rdf_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_scope_key text;
    v_object_kind text;
    v_object_id uuid;
    v_old_state jsonb;
BEGIN
    IF TG_TABLE_NAME='semantic_topology_node' THEN
        v_object_kind := 'node';
        v_scope_key := COALESCE(NEW.scope_key, OLD.scope_key);
        v_object_id := COALESCE(NEW.topology_node_id, OLD.topology_node_id);
        IF TG_OP <> 'INSERT' THEN
            v_old_state := jsonb_build_object(
                'scope_key', OLD.scope_key,
                'node_type', OLD.node_type,
                'node_key', OLD.node_key,
                'label', OLD.label
            );
        END IF;
    ELSIF TG_TABLE_NAME='semantic_topology_edge' THEN
        v_object_kind := 'edge';
        v_scope_key := COALESCE(NEW.scope_key, OLD.scope_key);
        v_object_id := COALESCE(NEW.edge_id, OLD.edge_id);
        IF TG_OP <> 'INSERT' THEN
            v_old_state := jsonb_build_object(
                'scope_key', OLD.scope_key,
                'parent_node_id', OLD.parent_node_id,
                'child_node_id', OLD.child_node_id,
                'edge_type', OLD.edge_type
            );
        END IF;
    ELSE
        v_object_kind := 'anchor';
        v_scope_key := COALESCE(NEW.source_scope_key, OLD.source_scope_key);
        v_object_id := COALESCE(NEW.anchor_edge_id, OLD.anchor_edge_id);
        IF TG_OP <> 'INSERT' THEN
            v_old_state := jsonb_build_object(
                'source_scope_key', OLD.source_scope_key,
                'source_node_id', OLD.source_node_id,
                'target_node_id', OLD.target_node_id,
                'relationship_type', OLD.relationship_type
            );
        END IF;
    END IF;

    INSERT INTO aios.semantic_rdf_change (
        scope_key, object_kind, object_id, operation, old_state
    ) VALUES (
        v_scope_key,
        v_object_kind,
        v_object_id,
        CASE WHEN TG_OP='DELETE' THEN 'delete' ELSE 'upsert' END,
        v_old_state
    );

    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_topology_node_rdf_change ON aios.semantic_topology_node;
CREATE TRIGGER trg_semantic_topology_node_rdf_change
AFTER INSERT OR UPDATE OR DELETE ON aios.semantic_topology_node
FOR EACH ROW EXECUTE FUNCTION aios.record_semantic_topology_rdf_change();

DROP TRIGGER IF EXISTS trg_semantic_topology_edge_rdf_change ON aios.semantic_topology_edge;
CREATE TRIGGER trg_semantic_topology_edge_rdf_change
AFTER INSERT OR UPDATE OR DELETE ON aios.semantic_topology_edge
FOR EACH ROW EXECUTE FUNCTION aios.record_semantic_topology_rdf_change();

DROP TRIGGER IF EXISTS trg_semantic_anchor_edge_rdf_change ON aios.semantic_anchor_edge;
CREATE TRIGGER trg_semantic_anchor_edge_rdf_change
AFTER INSERT OR UPDATE OR DELETE ON aios.semantic_anchor_edge
FOR EACH ROW EXECUTE FUNCTION aios.record_semantic_topology_rdf_change();

COMMIT;
