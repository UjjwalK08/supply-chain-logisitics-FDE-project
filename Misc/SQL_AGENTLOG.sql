CREATE TABLE FDE_VIEWS.AgentAuditLog (
    LogID        INT            IDENTITY (1, 1) PRIMARY KEY,
    Timestamp    DATETIME       DEFAULT GETDATE(),
    SessionID    VARCHAR (50)  ,
    NodeEexcuted VARCHAR (50)  ,
    ToolName     VARCHAR (100) ,
    Content      NVARCHAR (MAX) -- NVARCHAR to safely handle JSON string and Large LLM
);

GRANT INSERT
    ON FDE_VIEWS.AgentAuditLog TO USR_FDE_RO;