-- Ensure the deterministic default HUD profile required by runtime exists.
--
-- The canonical baseline is schema-only, so required runtime configuration must
-- be seeded by an immutable post-baseline migration.
BEGIN;

INSERT INTO aios.hud_profile (
    profile_name,
    description
)
VALUES (
    'default',
    'Default deterministic AIOS HUD profile.'
)
ON CONFLICT (profile_name) DO NOTHING;

INSERT INTO aios.character_hud_profile (
    character_id,
    profile_id
)
SELECT
    ci.character_id,
    hp.profile_id
FROM aios.character_identity ci
JOIN aios.hud_profile hp
    ON hp.profile_name = 'default'
ON CONFLICT (character_id) DO NOTHING;

COMMIT;
