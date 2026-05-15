-- =============================================================================
-- DUPLICATE ENTITIES ANALYSIS SCRIPT
-- Database: test_flippro_hostke
-- Purpose: Identify duplicate entities and gather data to build a merge workplan
-- =============================================================================

\set ON_ERROR_STOP on
\pset border 2
\pset format aligned

-- =============================================================================
-- SECTION 1: IDENTIFY ALL DUPLICATE ENTITIES
-- =============================================================================
\echo ''
\echo '======================================================================'
\echo 'SECTION 1: DUPLICATE ENTITIES'
\echo '======================================================================'

SELECT
    e.id,
    e.name,
    e.email,
    e.phone_number,
    e.status,
    e.client_key,
    e.is_onboarded,
    et.name        AS entity_type,
    e.physical_address,
    e.v_one_id
FROM entities e
LEFT JOIN entity_type et ON et.id = e.entity_type_id
WHERE e.name IN (
    SELECT name
    FROM entities
    GROUP BY name
    HAVING COUNT(*) > 1
)
ORDER BY e.name, e.status, e.id;


-- =============================================================================
-- SECTION 2: APPOINTMENTS PER DUPLICATE ENTITY
-- Appointments are linked to a service provider via service_provider_id (uuid)
-- and to a client (insurance) via client_key.
-- Since these entities are Garages (service providers) we count by service_provider_id.
-- =============================================================================
\echo ''
\echo '======================================================================'
\echo 'SECTION 2: APPOINTMENTS PER DUPLICATE ENTITY'
\echo '======================================================================'

WITH duplicate_names AS (
    SELECT name
    FROM entities
    GROUP BY name
    HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key, et.name AS entity_type
    FROM entities e
    LEFT JOIN entity_type et ON et.id = e.entity_type_id
    WHERE e.name IN (SELECT name FROM duplicate_names)
)
SELECT
    de.name                                          AS entity_name,
    de.id                                            AS entity_id,
    de.client_key,
    de.entity_type,
    COUNT(a.id)                                      AS total_appointments,
    COUNT(a.id) FILTER (WHERE a.task_status = 'COMPLETED')   AS completed,
    COUNT(a.id) FILTER (WHERE a.task_status = 'PENDING')     AS pending,
    COUNT(a.id) FILTER (WHERE a.task_status NOT IN ('COMPLETED','PENDING')) AS other_status
FROM duplicate_entities de
LEFT JOIN appointments a ON a.service_provider_id = de.id
GROUP BY de.name, de.id, de.client_key, de.entity_type
ORDER BY de.name, total_appointments DESC;


-- =============================================================================
-- SECTION 3: INSURANCE COMPANY MAPPINGS PER DUPLICATE ENTITY
-- entity_to_entity maps service providers (garages) to insurance companies.
-- =============================================================================
\echo ''
\echo '======================================================================'
\echo 'SECTION 3: INSURANCE COMPANY MAPPINGS (entity_to_entity)'
\echo '======================================================================'

-- 3a: Summary count
WITH duplicate_names AS (
    SELECT name FROM entities GROUP BY name HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key
    FROM entities e
    WHERE e.name IN (SELECT name FROM duplicate_names)
)
SELECT
    de.name                 AS entity_name,
    de.id                   AS entity_id,
    de.client_key,
    COUNT(ete.id)           AS mapped_insurance_count,
    COUNT(ete.id) FILTER (WHERE ete.status = 'ACTIVE')   AS active_mappings,
    COUNT(ete.id) FILTER (WHERE ete.status != 'ACTIVE')  AS inactive_mappings
FROM duplicate_entities de
LEFT JOIN entity_to_entity ete ON ete.service_provider_id = de.id
GROUP BY de.name, de.id, de.client_key
ORDER BY de.name, mapped_insurance_count DESC;

\echo ''
\echo '-- 3b: Full list of insurance mappings per duplicate entity --'

-- 3b: Full detail of each mapping
WITH duplicate_names AS (
    SELECT name FROM entities GROUP BY name HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key
    FROM entities e
    WHERE e.name IN (SELECT name FROM duplicate_names)
)
SELECT
    de.name                     AS sp_entity_name,
    de.id                       AS sp_entity_id,
    de.client_key               AS sp_client_key,
    ins.name                    AS insurance_name,
    ins.id                      AS insurance_id,
    ete.id                      AS mapping_id,
    ete.status                  AS mapping_status,
    ete.can_book_valuation,
    ete.approval_required_for_valuation_booking,
    ete.markup,
    ete.vatable
FROM duplicate_entities de
LEFT JOIN entity_to_entity ete ON ete.service_provider_id = de.id
LEFT JOIN entities ins ON ins.id = ete.insurance_id
ORDER BY de.name, ins.name;


-- =============================================================================
-- SECTION 4: BRANCHES PER DUPLICATE ENTITY
-- =============================================================================
\echo ''
\echo '======================================================================'
\echo 'SECTION 4: BRANCHES PER DUPLICATE ENTITY'
\echo '======================================================================'

-- 4a: Summary count
WITH duplicate_names AS (
    SELECT name FROM entities GROUP BY name HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key
    FROM entities e
    WHERE e.name IN (SELECT name FROM duplicate_names)
)
SELECT
    de.name               AS entity_name,
    de.id                 AS entity_id,
    de.client_key,
    COUNT(eb.id)          AS total_branches,
    COUNT(eb.id) FILTER (WHERE eb.status = 'ACTIVE')   AS active_branches,
    COUNT(eb.id) FILTER (WHERE eb.status != 'ACTIVE')  AS inactive_branches
FROM duplicate_entities de
LEFT JOIN entity_branches eb ON eb.entity_id = de.id
GROUP BY de.name, de.id, de.client_key
ORDER BY de.name, total_branches DESC;

\echo ''
\echo '-- 4b: Full branch details per duplicate entity --'

-- 4b: Full branch detail
WITH duplicate_names AS (
    SELECT name FROM entities GROUP BY name HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key
    FROM entities e
    WHERE e.name IN (SELECT name FROM duplicate_names)
)
SELECT
    de.name               AS entity_name,
    de.id                 AS entity_id,
    de.client_key,
    eb.id                 AS branch_id,
    eb.email              AS branch_email,
    eb.phone_number       AS branch_phone,
    eb.physical_address   AS branch_address,
    eb.status             AS branch_status,
    aa.name               AS admin_area
FROM duplicate_entities de
LEFT JOIN entity_branches eb ON eb.entity_id = de.id
LEFT JOIN admin_areas aa ON aa.id = eb.administrative_area_id
ORDER BY de.name, eb.status;


-- =============================================================================
-- SECTION 5: CONSOLIDATED WORKPLAN SUMMARY
-- Shows all data side-by-side to inform the merge decision.
-- =============================================================================
\echo ''
\echo '======================================================================'
\echo 'SECTION 5: CONSOLIDATED WORKPLAN SUMMARY (per duplicate group)'
\echo '======================================================================'

WITH duplicate_names AS (
    SELECT name FROM entities GROUP BY name HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key, e.status, e.email, e.phone_number,
           e.is_onboarded, et.name AS entity_type
    FROM entities e
    LEFT JOIN entity_type et ON et.id = e.entity_type_id
    WHERE e.name IN (SELECT name FROM duplicate_names)
),
appt_counts AS (
    SELECT a.service_provider_id AS entity_id, COUNT(*) AS appt_count
    FROM appointments a
    WHERE a.service_provider_id IN (SELECT id FROM duplicate_entities)
    GROUP BY a.service_provider_id
),
ete_counts AS (
    SELECT ete.service_provider_id AS entity_id, COUNT(*) AS insurance_count
    FROM entity_to_entity ete
    WHERE ete.service_provider_id IN (SELECT id FROM duplicate_entities)
    GROUP BY ete.service_provider_id
),
branch_counts AS (
    SELECT eb.entity_id, COUNT(*) AS branch_count
    FROM entity_branches eb
    WHERE eb.entity_id IN (SELECT id FROM duplicate_entities)
    GROUP BY eb.entity_id
)
SELECT
    de.name                             AS entity_name,
    de.id                               AS entity_id,
    de.client_key,
    de.status,
    de.entity_type,
    de.is_onboarded,
    COALESCE(ac.appt_count, 0)          AS appointments,
    COALESCE(ec.insurance_count, 0)     AS insurance_mappings,
    COALESCE(bc.branch_count, 0)        AS branches
FROM duplicate_entities de
LEFT JOIN appt_counts  ac ON ac.entity_id  = de.id
LEFT JOIN ete_counts   ec ON ec.entity_id  = de.id
LEFT JOIN branch_counts bc ON bc.entity_id = de.id
ORDER BY de.name, appointments DESC, insurance_mappings DESC;


-- =============================================================================
-- SECTION 6: ENTITIES TO REMOVE — RECORD FOR AUTH DB CLEANUP
-- The entity with fewer appointments / mappings is the candidate for removal.
-- This query flags the LOWER-priority duplicate in each group.
-- Review carefully before executing any DELETE.
-- =============================================================================
\echo ''
\echo '======================================================================'
\echo 'SECTION 6: REMOVAL CANDIDATES (review before acting)'
\echo '======================================================================'

WITH duplicate_names AS (
    SELECT name FROM entities GROUP BY name HAVING COUNT(*) > 1
),
duplicate_entities AS (
    SELECT e.id, e.name, e.client_key, e.status, e.email, e.phone_number, e.is_onboarded,
           et.name AS entity_type
    FROM entities e
    LEFT JOIN entity_type et ON et.id = e.entity_type_id
    WHERE e.name IN (SELECT name FROM duplicate_names)
),
appt_counts AS (
    SELECT a.service_provider_id AS entity_id, COUNT(*) AS appt_count
    FROM appointments a
    WHERE a.service_provider_id IN (SELECT id FROM duplicate_entities)
    GROUP BY a.service_provider_id
),
ete_counts AS (
    SELECT ete.service_provider_id AS entity_id, COUNT(*) AS insurance_count
    FROM entity_to_entity ete
    WHERE ete.service_provider_id IN (SELECT id FROM duplicate_entities)
    GROUP BY ete.service_provider_id
),
branch_counts AS (
    SELECT eb.entity_id, COUNT(*) AS branch_count
    FROM entity_branches eb
    WHERE eb.entity_id IN (SELECT id FROM duplicate_entities)
    GROUP BY eb.entity_id
),
ranked AS (
    SELECT
        de.name,
        de.id,
        de.client_key,
        de.status,
        de.email,
        de.phone_number,
        de.entity_type,
        de.is_onboarded,
        COALESCE(ac.appt_count, 0)      AS appointments,
        COALESCE(ec.insurance_count, 0) AS insurance_mappings,
        COALESCE(bc.branch_count, 0)    AS branches,
        ROW_NUMBER() OVER (
            PARTITION BY de.name
            ORDER BY
                COALESCE(ac.appt_count, 0)      DESC,
                COALESCE(ec.insurance_count, 0) DESC,
                COALESCE(bc.branch_count, 0)    DESC
        ) AS priority_rank
    FROM duplicate_entities de
    LEFT JOIN appt_counts   ac ON ac.entity_id  = de.id
    LEFT JOIN ete_counts    ec ON ec.entity_id  = de.id
    LEFT JOIN branch_counts bc ON bc.entity_id  = de.id
)
SELECT
    name            AS entity_name,
    id              AS entity_id_to_remove,
    client_key      AS client_key_to_remove,
    status,
    email,
    phone_number,
    entity_type,
    is_onboarded,
    appointments,
    insurance_mappings,
    branches,
    'KEEP'          AS action
FROM ranked WHERE priority_rank = 1

UNION ALL

SELECT
    name            AS entity_name,
    id              AS entity_id_to_remove,
    client_key      AS client_key_to_remove,
    status,
    email,
    phone_number,
    entity_type,
    is_onboarded,
    appointments,
    insurance_mappings,
    branches,
    'REMOVE'        AS action
FROM ranked WHERE priority_rank > 1

ORDER BY entity_name, action;
