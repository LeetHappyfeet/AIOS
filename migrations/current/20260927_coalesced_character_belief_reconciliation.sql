-- Coalesce character belief reconciliation behind a durable dirty set.
-- 2026-09-27
--
-- Evidence/admission/topology writes used to run the full belief reconciler
-- synchronously from row triggers. One acquisition could therefore reconcile
-- the same (instance, atom) several times while it moved through enrichment.
-- PostgreSQL remains authoritative; triggers now only invalidate affected
-- coordinates and a bounded pipeline stage performs the existing serialized
-- reconciliation later.

BEGIN;

CREATE TABLE IF NOT EXISTS aios.character_belief_reconciliation_dirty (
    instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id) ON DELETE CASCADE,
    atom_id uuid NOT NULL,
    dirty_version bigint NOT NULL DEFAULT 1,
    dirty_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, atom_id)
);

CREATE INDEX IF NOT EXISTS idx_character_belief_reconciliation_dirty_age
    ON aios.character_belief_reconciliation_dirty (dirty_at, instance_id, atom_id);

COMMENT ON TABLE aios.character_belief_reconciliation_dirty IS
'Coalescing invalidation set for character belief reconciliation. Triggers record the latest dirty version; the pipeline reconciles each affected instance/atom coordinate outside the originating write transaction.';

CREATE OR REPLACE FUNCTION aios.mark_character_belief_dirty_descendants(
    p_source_instance_id uuid,
    p_atom_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_source_instance_id IS NULL OR p_atom_id IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO aios.character_belief_reconciliation_dirty (
        instance_id, atom_id, dirty_version, dirty_at
    )
    WITH RECURSIVE descendants AS (
        SELECT ci.instance_id
        FROM aios.character_instance ci
        WHERE ci.instance_id=p_source_instance_id

        UNION ALL

        SELECT child.instance_id
        FROM aios.character_instance child
        JOIN descendants parent
          ON child.parent_instance_id=parent.instance_id
    )
    SELECT instance_id, p_atom_id, 1, now()
    FROM descendants
    ON CONFLICT (instance_id, atom_id) DO UPDATE
    SET dirty_version=aios.character_belief_reconciliation_dirty.dirty_version + 1,
        dirty_at=now();
END;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_character_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_proposition_id uuid;
    v_atom_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        v_instance_id := OLD.instance_id;
        v_proposition_id := OLD.proposition_id;
    ELSE
        v_instance_id := NEW.instance_id;
        v_proposition_id := NEW.proposition_id;
    END IF;

    SELECT atom_id INTO v_atom_id
    FROM aios.proposition
    WHERE proposition_id=v_proposition_id;

    PERFORM aios.mark_character_belief_dirty_descendants(v_instance_id, v_atom_id);
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$;

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_admission()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_acquisition_id uuid;
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    v_acquisition_id := CASE WHEN TG_OP='DELETE'
        THEN OLD.acquisition_id ELSE NEW.acquisition_id END;

    SELECT kae.instance_id, p.atom_id
    INTO v_instance_id, v_atom_id
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
    WHERE kae.acquisition_id=v_acquisition_id;

    PERFORM aios.mark_character_belief_dirty_descendants(v_instance_id, v_atom_id);
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$;

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_acquisition_topology()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_instance_id uuid;
    v_atom_id uuid;
BEGIN
    SELECT kae.instance_id, p.atom_id
    INTO v_instance_id, v_atom_id
    FROM aios.knowledge_acquisition_event kae
    JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
    WHERE kae.acquisition_id=NEW.acquisition_id;

    PERFORM aios.mark_character_belief_dirty_descendants(v_instance_id, v_atom_id);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION aios.refresh_belief_from_ingest_supersession()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
BEGIN
    IF OLD.superseded_at IS NOT DISTINCT FROM NEW.superseded_at THEN
        RETURN NEW;
    END IF;

    FOR rec IN
        SELECT DISTINCT kae.instance_id, p.atom_id
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.proposition p ON p.proposition_id=kae.proposition_id
        JOIN aios.dag_node dn ON dn.node_id=kae.dag_node_id
        WHERE dn.event_id=NEW.event_id
          AND p.atom_id IS NOT NULL
    LOOP
        PERFORM aios.mark_character_belief_dirty_descendants(rec.instance_id, rec.atom_id);
    END LOOP;

    RETURN NEW;
END;
$$;

-- Existing materialized beliefs may have been left between trigger-driven
-- passes during deployment. Seed one idempotent reconciliation pass for them.
INSERT INTO aios.character_belief_reconciliation_dirty (
    instance_id, atom_id, dirty_version, dirty_at
)
SELECT instance_id, atom_id, 1, now()
FROM aios.character_belief_state
ON CONFLICT (instance_id, atom_id) DO NOTHING;

COMMIT;
