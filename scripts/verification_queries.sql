-- ====================================================================
-- FDE VERIFICATION QUERIES
-- Ad-hoc checks used after each setup phase. Not part of any pipeline -
-- run these by hand to confirm a phase landed correctly.
-- ====================================================================

-- --------------------------------------------------------------------
-- PHASE 0: Legacy ingestion (run as ADMIN / sa)
-- After: python scripts/ingest_legacy_data.py
-- --------------------------------------------------------------------
SELECT COUNT(*) AS total_rows
FROM   dbo.TBL_SC_FLEET_HIST_RAW;
-- Expect: ~32,065 rows from the Kaggle logistics dataset

SELECT TOP 100 *
FROM   dbo.TBL_SC_FLEET_HIST_RAW;
-- Expect: the deliberately "legacy" column names (TS_UTC, V_LAT, IOT_TEMP_VAL_C, ...)


-- --------------------------------------------------------------------
-- PHASE 2: Security boundary (run as the AGENT user, USR_FDE_RO)
-- After: scripts/setup_security_and_view.sql
-- Connect with the agent-fde-ro profile, NOT the admin profile, or both
-- of these will succeed and the test proves nothing.
-- --------------------------------------------------------------------

-- TEST 1: SHOULD SUCCEED - the clean semantic view is the agent's only surface
SELECT TOP 5 *
FROM   FDE_VIEWS.VW_ACTIVE_FLEET;

-- TEST 2: SHOULD FAIL INSTANTLY - raw legacy table is explicitly DENIED
SELECT TOP 5 *
FROM   dbo.TBL_SC_FLEET_HIST_RAW;
-- Expect: "The SELECT permission was denied on the object 'TBL_SC_FLEET_HIST_RAW'"

-- TEST 3: SHOULD FAIL - the agent has no write access to the legacy schema
UPDATE dbo.TBL_SC_FLEET_HIST_RAW
SET    IOT_TEMP_VAL_C = 0;
-- Expect: permission denied. This is the guardrail against LLM-generated mutations.


-- --------------------------------------------------------------------
-- PHASE 4: Audit trail (run as ADMIN / sa)
-- After: scripts/setup_audit_log.sql, and at least one query in the UI
-- --------------------------------------------------------------------
SELECT TOP 50 LogID,
              Timestamp,
              SessionID,
              NodeExecuted,
              ToolName,
              LEFT(Content, 200) AS ContentPreview
FROM   FDE_VIEWS.AgentAuditLog
ORDER  BY Timestamp DESC;
-- Expect: one row per tool call (NodeExecuted = 'reasoner' / 'tools')
--         plus one 'reasoner_final' row per answer.
-- Empty table after running a query? The INSERT is failing silently -
-- re-run scripts/setup_audit_log.sql and check the column spelling.
