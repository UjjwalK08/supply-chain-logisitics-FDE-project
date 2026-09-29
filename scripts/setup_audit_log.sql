-- ====================================================================
-- FDE PHASE 4: AGENT AUDIT LOG
-- Purpose: Persist every reasoning step and tool invocation the agent
--          makes, so LLM behaviour is reviewable after the fact.
-- Run as:  the ADMIN login (sa / SQL_ADMIN_USERNAME), not the agent user.
-- Prereq:  scripts/setup_security_and_view.sql (creates FDE_VIEWS + USR_FDE_RO)
-- ====================================================================
-- --------------------------------------------------------------------
-- 0. MIGRATION: repair a pre-existing table with the misspelled column
-- --------------------------------------------------------------------
-- An earlier version of this DDL created the column as [NodeEexcuted].
-- src/ui.py inserts into [NodeExecuted], so against that older table every
-- audit INSERT fails. write_audit_log() swallows the exception and only
-- prints it, so the UI looks healthy while the table stays empty -- and the
-- audit viewer fails with "Invalid column name 'NodeExecuted'".
--
-- Renaming in place rather than dropping: non-destructive, preserves any
-- existing rows, and leaves the existing GRANT INSERT intact.
IF EXISTS (SELECT 1
           FROM   sys.columns
           WHERE  object_id = OBJECT_ID('FDE_VIEWS.AgentAuditLog')
                  AND name = 'NodeEexcuted')
    BEGIN
        PRINT 'Legacy audit table found with misspelled column - renaming to NodeExecuted.';
        EXECUTE sp_rename 'FDE_VIEWS.AgentAuditLog.NodeEexcuted', 'NodeExecuted', 'COLUMN';
    END


GO
-- --------------------------------------------------------------------
-- 1. Create the audit table
-- --------------------------------------------------------------------
IF OBJECT_ID('FDE_VIEWS.AgentAuditLog', 'U') IS NULL
    BEGIN
        CREATE TABLE FDE_VIEWS.AgentAuditLog (
            LogID        INT            IDENTITY (1, 1) PRIMARY KEY,
            Timestamp    DATETIME       DEFAULT GETDATE(),
            SessionID    VARCHAR (50)  ,
            NodeExecuted VARCHAR (50)  ,
            ToolName     VARCHAR (100) ,
            Content      NVARCHAR (MAX) -- NVARCHAR to safely hold JSON args and large LLM outputs
        );
        PRINT 'FDE_VIEWS.AgentAuditLog created.';
    END
ELSE
    PRINT 'FDE_VIEWS.AgentAuditLog already exists - skipping create.';


GO
-- --------------------------------------------------------------------
-- 2. Grant the agent write-only access to this one table
-- --------------------------------------------------------------------
-- USR_FDE_RO stays read-only everywhere else. This is the single, narrow
-- exception that lets the agent record its own activity.
GRANT INSERT
    ON FDE_VIEWS.AgentAuditLog TO USR_FDE_RO;


GO
-- --------------------------------------------------------------------
-- 3. Verify
-- --------------------------------------------------------------------
SELECT   name AS ColumnName,
         TYPE_NAME(user_type_id) AS DataType
FROM     sys.columns
WHERE    object_id = OBJECT_ID('FDE_VIEWS.AgentAuditLog')
ORDER BY column_id;


-- Expect: LogID, Timestamp, SessionID, NodeExecuted, ToolName, Content