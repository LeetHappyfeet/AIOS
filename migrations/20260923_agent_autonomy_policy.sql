-- Autonomy policy and deterministic-vs-LLM execution choice.

BEGIN;

ALTER TABLE aios.character_cognitive_task
    ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'auto'
        CHECK (execution_mode IN ('auto','deterministic','llm'));

ALTER TABLE aios.character_agent_runtime
    ADD COLUMN IF NOT EXISTS max_semantic_wake_chain integer NOT NULL DEFAULT 4
        CHECK (max_semantic_wake_chain BETWEEN 1 AND 64),
    ADD COLUMN IF NOT EXISTS max_inference_per_task integer NOT NULL DEFAULT 4
        CHECK (max_inference_per_task BETWEEN 1 AND 64),
    ADD COLUMN IF NOT EXISTS max_actions_per_task integer NOT NULL DEFAULT 8
        CHECK (max_actions_per_task BETWEEN 1 AND 128),
    ADD COLUMN IF NOT EXISTS cooldown_seconds integer NOT NULL DEFAULT 5
        CHECK (cooldown_seconds BETWEEN 0 AND 86400),
    ADD COLUMN IF NOT EXISTS cooldown_until timestamptz NULL,
    ADD COLUMN IF NOT EXISTS semantic_wake_chain integer NOT NULL DEFAULT 0;

COMMIT;
