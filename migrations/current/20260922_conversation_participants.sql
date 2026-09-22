-- Multi-party conversation participation and per-message perception.
BEGIN;
CREATE TABLE IF NOT EXISTS aios.conversation_participant (
 participant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 timeline_id uuid NOT NULL REFERENCES aios.timeline(timeline_id) ON DELETE CASCADE,
 character_id text REFERENCES aios.character_identity(character_id) ON DELETE SET NULL,
 character_instance_id uuid REFERENCES aios.character_instance(instance_id) ON DELETE SET NULL,
 source_actor_id text NOT NULL,
 actor_type aios.actor_type NOT NULL DEFAULT 'character',
 controller_type text, controller_ref text,
 participant_role text NOT NULL DEFAULT 'participant',
 joined_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
 left_node_id uuid REFERENCES aios.dag_node(node_id) ON DELETE SET NULL,
 perception_mode text NOT NULL DEFAULT 'present',
 active boolean NOT NULL DEFAULT true,
 meta jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (timeline_id, source_actor_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_participant_character ON aios.conversation_participant(character_id,timeline_id) WHERE character_id IS NOT NULL AND active;
CREATE INDEX IF NOT EXISTS idx_conversation_participant_instance ON aios.conversation_participant(character_instance_id,timeline_id) WHERE character_instance_id IS NOT NULL AND active;
CREATE TABLE IF NOT EXISTS aios.message_participant (
 node_id uuid NOT NULL REFERENCES aios.dag_node(node_id) ON DELETE CASCADE,
 participant_id uuid NOT NULL REFERENCES aios.conversation_participant(participant_id) ON DELETE CASCADE,
 relation text NOT NULL CHECK (relation IN ('speaker','addressee','audience','mentioned','observer')),
 perceived boolean NOT NULL DEFAULT true,
 meta jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (node_id,participant_id,relation)
);
CREATE INDEX IF NOT EXISTS idx_message_participant_perceiver ON aios.message_participant(participant_id,node_id) WHERE perceived;
INSERT INTO aios.conversation_participant(timeline_id,character_id,source_actor_id,actor_type,controller_type,controller_ref,participant_role,meta)
SELECT t.timeline_id,ci.character_id,t.character_id,'character'::aios.actor_type,'agent','character:'||t.character_id,'primary',jsonb_build_object('backfilled_from','timeline.character_id')
FROM aios.timeline t JOIN aios.character_identity ci ON ci.character_id=t.character_id
WHERE t.character_id IS NOT NULL ON CONFLICT (timeline_id,source_actor_id) DO NOTHING;
INSERT INTO aios.conversation_participant(timeline_id,source_actor_id,actor_type,controller_type,controller_ref,participant_role,meta)
SELECT t.timeline_id,t.user_name,'user'::aios.actor_type,'human',t.user_name,'participant',jsonb_build_object('backfilled_from','timeline.user_name')
FROM aios.timeline t WHERE t.user_name IS NOT NULL AND btrim(t.user_name)<>'' ON CONFLICT (timeline_id,source_actor_id) DO NOTHING;
COMMENT ON TABLE aios.conversation_participant IS 'Participants in a shared conversation/source timeline. source_actor_id preserves source provenance; character_id binds that actor to durable AIOS cognition when known.';
COMMENT ON TABLE aios.message_participant IS 'Per-message speaker/addressee/audience/observer relations and perception boundary for multi-party cognition.';
COMMIT;
