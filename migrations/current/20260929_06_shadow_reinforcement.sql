BEGIN;
CREATE TABLE aios.character_reinforcement_event (
 reinforcement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 outcome_id uuid NOT NULL REFERENCES aios.character_outcome_event(outcome_id),
 target_type text NOT NULL CHECK (target_type IN ('strategy','observation')),
 target_key text NOT NULL,
 signal_type text NOT NULL,
 magnitude double precision NOT NULL CHECK (magnitude BETWEEN -1 AND 1),
 policy_version text NOT NULL,
 reason text NOT NULL,
 shadow boolean NOT NULL DEFAULT true CHECK (shadow),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(outcome_id,target_type,target_key,signal_type,policy_version)
);
CREATE TABLE aios.character_behavior_projection (
 instance_id uuid NOT NULL REFERENCES aios.character_instance(instance_id),
 scope_key text NOT NULL,
 strategy_key text NOT NULL,
 policy_version text NOT NULL,
 success_count integer NOT NULL CHECK (success_count>=0),
 failure_count integer NOT NULL CHECK (failure_count>=0),
 signal_mean double precision NOT NULL CHECK (signal_mean BETWEEN -1 AND 1),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(instance_id,scope_key,strategy_key,policy_version)
);
CREATE TRIGGER immutable_character_reinforcement BEFORE UPDATE OR DELETE ON aios.character_reinforcement_event
 FOR EACH ROW EXECUTE FUNCTION aios.reject_outcome_ledger_mutation();
COMMIT;
