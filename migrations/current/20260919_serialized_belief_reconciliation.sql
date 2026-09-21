-- Serialize character belief reconciliation across activation and background workers.
BEGIN;

CREATE OR REPLACE FUNCTION aios.reconcile_character_belief_atom_serialized(
    p_instance_id uuid,
    p_atom_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_character_id text;
BEGIN
    SELECT ci.character_id
    INTO v_character_id
    FROM aios.character_instance ci
    WHERE ci.instance_id=p_instance_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- character_belief_state rows are instance-local, but reconciliation also
    -- mutates the shared char:<character_id> semantic topology. Use one
    -- transaction-scoped lock for that shared ownership partition so workers
    -- cannot enter the topology materializer in conflicting row orders.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('belief-character:' || v_character_id, 0)
    );

    PERFORM aios.reconcile_character_belief_atom(p_instance_id, p_atom_id);
END;
$$;

COMMENT ON FUNCTION aios.reconcile_character_belief_atom_serialized(uuid, uuid) IS
'Concurrency-safe entry point for character belief reconciliation. Serializes the shared character topology partition before invoking the atom materializer.';

COMMIT;
