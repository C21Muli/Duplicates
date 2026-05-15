-- =============================================================================
-- DUPLICATE ENTITIES WORKPLAN
-- Database: test_flippro_hostke
-- Generated: 2026-05-15
--
-- FINDINGS SUMMARY
-- ─────────────────────────────────────────────────────────────────────────────
-- Entity name       : BHATTI PANEL BEATERS LTD
-- Entity type       : Garage
--
-- KEEP   id         : ea736d14-fd42-4dbb-97e0-f871b2094f96
--        client_key : BHATTI
--        Appointments      : 181
--        Insurance mappings: 2  (Geminia Insurance Co Ltd, Old Mutual Kenya)
--        Branches          : 1  (Nairobi – ACTIVE)
--
-- REMOVE id         : 4ae1213f-b9a1-42eb-86ea-44c03c317411
--        client_key : SP-KUKZFWLILG
--        Appointments      : 0
--        Insurance mappings: 0
--        Branches          : 1  (Nairobi – ACTIVE, no address/email/phone)
--
-- PLAN
-- 1. Log the entity to remove into a tracking table for auth-DB cleanup.
-- 2. Transfer the branch from the entity to remove to the entity to keep
--    (only if the branch carries unique data; otherwise delete it).
-- 3. Remove the duplicate entity.
--
-- HOW TO RUN
--   psql -d test_flippro_hostke -f duplicate_entities_workplan.sql
--
-- The entire workplan runs inside a single transaction.
-- Call ROLLBACK instead of COMMIT to do a dry-run and inspect changes.
-- =============================================================================

\set ON_ERROR_STOP on
\pset border 2
\pset format aligned

BEGIN;

-- ---------------------------------------------------------------------------
-- STEP 0: Create a tracking table for removed entities
--         (idempotent – safe to run again if interrupted)
-- ---------------------------------------------------------------------------
\echo ''
\echo '[STEP 0] Creating removed_entities log table (if not exists)...'

CREATE TABLE IF NOT EXISTS removed_entities (
    id              uuid                     PRIMARY KEY DEFAULT gen_random_uuid(),
    removed_entity_id   uuid                 NOT NULL,
    removed_client_key  character varying(50),
    entity_name         character varying(255),
    entity_type         character varying(255),
    kept_entity_id      uuid                 NOT NULL,
    kept_client_key     character varying(50),
    reason              text,
    removed_at          timestamp with time zone NOT NULL DEFAULT now(),
    resolved_in_auth_db boolean              NOT NULL DEFAULT false,
    notes               text
);

COMMENT ON TABLE removed_entities IS
    'Tracks entities deleted during duplicate-resolution. '
    'Used to identify affected users in the auth database.';

-- ---------------------------------------------------------------------------
-- STEP 1: Record the entity to be removed
-- ---------------------------------------------------------------------------
\echo '[STEP 1] Recording removal candidate in removed_entities...'

INSERT INTO removed_entities (
    removed_entity_id,
    removed_client_key,
    entity_name,
    entity_type,
    kept_entity_id,
    kept_client_key,
    reason
)
SELECT
    remove_e.id,
    remove_e.client_key,
    remove_e.name,
    et.name,
    keep_e.id,
    keep_e.client_key,
    'Duplicate entity — zero appointments and no insurance mappings; '
    'primary record (client_key=BHATTI) retained.'
FROM entities remove_e
JOIN entities keep_e   ON keep_e.id   = 'ea736d14-fd42-4dbb-97e0-f871b2094f96'
LEFT JOIN entity_type et ON et.id     = remove_e.entity_type_id
WHERE remove_e.id = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
ON CONFLICT (id) DO NOTHING;   -- safe if workplan is re-run

SELECT removed_entity_id, removed_client_key, entity_name, kept_entity_id, kept_client_key
FROM removed_entities
WHERE removed_entity_id = '4ae1213f-b9a1-42eb-86ea-44c03c317411';


-- ---------------------------------------------------------------------------
-- STEP 2: Inspect the branch on the entity to remove before acting
-- ---------------------------------------------------------------------------
\echo '[STEP 2] Branch on entity to remove (SP-KUKZFWLILG)...'

SELECT eb.id, eb.email, eb.phone_number, eb.physical_address, eb.status,
       aa.name AS admin_area
FROM entity_branches eb
LEFT JOIN admin_areas aa ON aa.id = eb.administrative_area_id
WHERE eb.entity_id = '4ae1213f-b9a1-42eb-86ea-44c03c317411';

\echo '[STEP 2] Branch on entity to keep (BHATTI)...'

SELECT eb.id, eb.email, eb.phone_number, eb.physical_address, eb.status,
       aa.name AS admin_area
FROM entity_branches eb
LEFT JOIN admin_areas aa ON aa.id = eb.administrative_area_id
WHERE eb.entity_id = 'ea736d14-fd42-4dbb-97e0-f871b2094f96';


-- ---------------------------------------------------------------------------
-- STEP 3: Handle the branch
--
-- The branch on SP-KUKZFWLILG has no email, phone, or address — it carries
-- no unique data not already on the BHATTI entity's branch (also Nairobi).
-- We delete it (CASCADE will clean up any mapping_branches rows).
--
-- If the branch DID carry unique contact data, replace the DELETE with:
--   UPDATE entity_branches SET entity_id = 'ea736d14-...' WHERE entity_id = '4ae1213f-...';
-- ---------------------------------------------------------------------------
\echo '[STEP 3] Removing empty branch from entity to be deleted...'

DELETE FROM entity_branches
WHERE entity_id = '4ae1213f-b9a1-42eb-86ea-44c03c317411';

-- Confirm
SELECT COUNT(*) AS remaining_branches_on_removed_entity
FROM entity_branches
WHERE entity_id = '4ae1213f-b9a1-42eb-86ea-44c03c317411';


-- ---------------------------------------------------------------------------
-- STEP 4: Verify no remaining FK-blocking data on the entity to remove
--         before attempting the DELETE.
-- ---------------------------------------------------------------------------
\echo '[STEP 4] Checking for any remaining references to entity to remove...'

-- Note: entity_entity_types FK has ON DELETE CASCADE — it will be cleaned up
--       automatically when the entity is deleted. Non-zero count here is expected.
SELECT 'appointments (sp)'    AS ref_table, COUNT(*) AS count FROM appointments       WHERE service_provider_id = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'entity_to_entity (sp)',               COUNT(*)          FROM entity_to_entity     WHERE service_provider_id = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'entity_to_entity (ins)',              COUNT(*)          FROM entity_to_entity     WHERE insurance_id        = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'entity_branches',                     COUNT(*)          FROM entity_branches      WHERE entity_id           = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'entity_lobs',                         COUNT(*)          FROM entity_lobs           WHERE entity_id          = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'entity_entity_types (CASCADE)',       COUNT(*)          FROM entity_entity_types   WHERE entity_id          = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'dashboard_cards',                     COUNT(*)          FROM dashboard_cards        WHERE entity_id         = '4ae1213f-b9a1-42eb-86ea-44c03c317411'
UNION ALL
SELECT 'entities (parent)',                   COUNT(*)          FROM entities               WHERE parent_entity_id  = '4ae1213f-b9a1-42eb-86ea-44c03c317411';


-- ---------------------------------------------------------------------------
-- STEP 5: Delete the duplicate entity
-- ---------------------------------------------------------------------------
\echo '[STEP 5] Deleting duplicate entity (SP-KUKZFWLILG)...'

DELETE FROM entities
WHERE id = '4ae1213f-b9a1-42eb-86ea-44c03c317411';

-- Confirm it is gone
SELECT COUNT(*) AS entities_deleted
FROM entities
WHERE id = '4ae1213f-b9a1-42eb-86ea-44c03c317411';


-- ---------------------------------------------------------------------------
-- STEP 6: Confirm the surviving entity is healthy
-- ---------------------------------------------------------------------------
\echo '[STEP 6] Confirming surviving entity (BHATTI)...'

SELECT
    e.id, e.name, e.client_key, e.status, e.email, e.phone_number, e.is_onboarded,
    et.name AS entity_type
FROM entities e
LEFT JOIN entity_type et ON et.id = e.entity_type_id
WHERE e.id = 'ea736d14-fd42-4dbb-97e0-f871b2094f96';

\echo '[STEP 6] Appointments on surviving entity...'
SELECT COUNT(*) AS appointments FROM appointments WHERE service_provider_id = 'ea736d14-fd42-4dbb-97e0-f871b2094f96';

\echo '[STEP 6] Insurance mappings on surviving entity...'
SELECT ete.id, ins.name AS insurance, ete.status
FROM entity_to_entity ete
JOIN entities ins ON ins.id = ete.insurance_id
WHERE ete.service_provider_id = 'ea736d14-fd42-4dbb-97e0-f871b2094f96';

\echo '[STEP 6] Branches on surviving entity...'
SELECT eb.id, eb.status, aa.name AS admin_area
FROM entity_branches eb
LEFT JOIN admin_areas aa ON aa.id = eb.administrative_area_id
WHERE eb.entity_id = 'ea736d14-fd42-4dbb-97e0-f871b2094f96';


-- ---------------------------------------------------------------------------
-- STEP 7: Final removed_entities log — hand off to auth-DB team
-- ---------------------------------------------------------------------------
\echo ''
\echo '[STEP 7] Removed entities log (share with auth-DB team)...'

SELECT
    removed_entity_id,
    removed_client_key,
    entity_name,
    entity_type,
    kept_entity_id,
    kept_client_key,
    removed_at,
    resolved_in_auth_db,
    reason
FROM removed_entities
ORDER BY removed_at;


-- =============================================================================
-- COMMIT or ROLLBACK
-- Review the output above, then:
--   • Type COMMIT; to apply all changes permanently.
--   • Type ROLLBACK; to undo everything (dry-run mode).
-- =============================================================================
\echo ''
\echo 'Review the output above.'
\echo 'Run COMMIT; to persist, or ROLLBACK; to undo.'

-- COMMIT;
