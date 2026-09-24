-- TEST 1: This SHOULD work perfectly (Access to clean view)
SELECT TOP 5 *
FROM   FDE_VIEWS.VW_ACTIVE_FLEET;

-- TEST 2: This SHOULD fail instantly (Access to raw legacy table is DENIED)
SELECT TOP 5 *
FROM   dbo.TBL_SC_FLEET_HIST_RAW;