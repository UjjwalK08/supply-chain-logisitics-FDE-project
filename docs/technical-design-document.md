# Technical Design Document (TDD): Cold Chain Control Tower

**Author:** Ujjwal Khanna
**Status:** Architecture Review
**Date:** September 2026
**Repository:** https://github.com/UjjwalK08/supply-chain-logisitics-FDE-project

---

## Table of Contents

1. System Architecture (HLD)
   - 1.1 Architectural Overview
   - 1.2 Component Responsibilities
2. Agent Orchestration (LLD)
   - 2.1 State Management & Execution Loop
   - 2.2 Model Routing & Provider Independence
   - 2.3 Resiliency & Fallbacks
3. Data & Security Model (LLD)
   - 3.1 Telemetry Security (VW_ACTIVE_FLEET)
   - 3.2 Role-Based Access Control (RBAC)
   - 3.3 Auditing
4. Retrieval Layer (LLD)
   - 4.1 Document Ingestion Pipeline
   - 4.2 Index Isolation
5. Presentation Tier
   - 5.1 Workspaces
   - 5.2 Output Contract
6. Deployment Strategy
   - 6.1 Infrastructure Topology
   - 6.2 Network Perimeter Security
7. Known Limitations

---

## 1. System Architecture (HLD)

The Cold Chain Control Tower is a decoupled, three-tier platform that lets control-tower
operators query fleet telemetry, environmental conditions, and compliance SOPs through a
natural-language interface. It performs **Text-to-SQL** over a governed view, calls
external APIs, and retrieves governing policy clauses, then synthesises the three into a
cited action plan.

### 1.1 Architectural Overview

A stateless presentation layer, a state-machine orchestration engine, and a hardened data
access layer.

```
                     Dispatcher (Streamlit, port 8501)
                                   |
                                   v
                  LangGraph StateGraph  <-----------+
                  "reasoner" node                   | loops until the model
                                   |                | stops requesting tools
                                   v                |
                     ToolNode (prebuilt) -----------+
                                   |
        +--------------------------+--------------------------+
        v                          v                          v
  query_telemetry_db    fetch_corridor_conditions    search_compliance_sop
        |                          |                          |
        v                          v                          v
  MSSQL 2022 (Docker)        open-meteo.com            Pinecone serverless
  FDE_VIEWS.VW_ACTIVE_FLEET  public REST API           SOP vector index
  read-only semantic view
        |
        +------------------> FDE_VIEWS.AgentAuditLog
                             (INSERT-only for the agent identity)
```

### 1.2 Component Responsibilities

- **Presentation Tier** — Streamlit application (`src/ui.py`). Holds UI session state only:
  a per-browser thread identifier, rendered message history, and cached dashboard queries.
  All reasoning is delegated to the orchestration tier.
- **Orchestration Engine** — A LangGraph `StateGraph` (`src/orchestrator.py`). Manages the
  execution lifecycle: intent deconstruction, tool selection, iteration, and synthesis of
  tool output into a structured report. This is a hand-assembled graph, not
  `create_react_agent`; the two-node loop is explicit and inspectable.
- **Data & Integration Tier** — Decoupled backends. The agent is isolated from raw data by
  a database view and a least-privilege role, so tier boundaries are enforced by the
  database engine rather than by application code.

---

## 2. Agent Orchestration (LLD)

The orchestration tier constrains probabilistic behaviour with a deterministic state
machine.

### 2.1 State Management & Execution Loop

State is a single `messages` list under LangGraph's `add_messages` reducer. The graph has
two nodes and one conditional edge:

| Element | Implementation |
|---|---|
| `reasoner` | Invokes the tool-bound LLM with the system prompt prepended at call time |
| `tools` | LangGraph's prebuilt `ToolNode` over the three tool functions |
| Routing | `tools_condition` — routes to `tools` if the reply contains tool calls, else to `END` |
| Persistence | `MemorySaver` checkpointer, keyed by `thread_id` (one per browser session) |

Execution sequence:

1. **Ingestion** — The dispatcher's message is appended to graph state.
2. **Reasoning** — The reasoner sees the system prompt plus full conversation history and
   either emits tool calls or produces a final answer. Tool choice is the model's
   decision; nothing in the graph forces a particular tool or order.
3. **Tool execution** — `ToolNode` runs every requested call and appends one `ToolMessage`
   per `tool_call_id`.
4. **Iteration** — Control returns to the reasoner, which may call further tools using the
   results just returned. This is what allows multi-hop questions: a location query feeds
   coordinates into a weather lookup, whose output is then checked against the SOP.
5. **Synthesis** — When no further tools are needed, the reasoner emits the final report.

The system prompt is prepended inside the reasoner node rather than seeded into stored
state. This guarantees every entry point (the Streamlit console and the CLI harness) is
governed by the same contract, and keeps the prompt out of the persisted history.

### 2.2 Model Routing & Provider Independence

Both the reasoner and the embedding model are selected at runtime by environment variable,
so the stack runs fully hosted or entirely local without code changes.

| Variable | Values | Effect |
|---|---|---|
| `Agent_llm` | `OPENAI` / `DEEPSEEK` / anything else | GPT-4o, DeepSeek, or a local Ollama fallback |
| `Embeddings_model` | `OPENAI` / `LOCAL` | 1536-dim hosted, or 1024-dim local BGE-M3 |

### 2.3 Resiliency & Fallbacks

- **Graceful degradation** — Each tool catches its own exceptions and returns a
  descriptive string rather than raising. A failed weather call or database error becomes
  context the model can reason about and report, instead of an unhandled crash.
- **Dashboard isolation** — The fleet snapshot powering the KPI tiles and map fails
  independently of the chat. If the database is unreachable the dashboard hides itself and
  surfaces the underlying error; the conversational path stays available.
- **Thread recovery** — If a run is interrupted between the reasoner and the tool node, the
  checkpoint retains an assistant message with tool calls but no matching tool results.
  Replaying that history is rejected by any OpenAI-compatible API, permanently poisoning
  the thread. The presentation tier detects this specific rejection, rotates to a fresh
  thread, and retries once. The chat input is additionally disabled while a run is in
  flight, which prevents the condition arising in the first place.

---

## 3. Data & Security Model (LLD)

Security is enforced at the database layer, so a prompt-injected or hallucinated
destructive statement is rejected by the server rather than being filtered in Python.

### 3.1 Telemetry Security (VW_ACTIVE_FLEET)

The agent interacts solely with the read-only view `FDE_VIEWS.VW_ACTIVE_FLEET`. The
`query_telemetry_db` tool generates T-SQL directly from the dispatcher's question; the
view is what makes that Text-to-SQL step tractable, because it renames a deliberately
legacy schema into readable English so the model can reason without a schema dictionary.
Generation accuracy and blast radius are therefore controlled by the same artefact.

| View column | Underlying legacy column | Type |
|---|---|---|
| `Timestamp` | `TS_UTC` | datetime |
| `Latitude` | `V_LAT` | float |
| `Longitude` | `V_LON` | float |
| `Current_Temperature_C` | `IOT_TEMP_VAL_C` | float (explicitly cast) |
| `Cargo_Condition_Code` | `CGO_COND_CD` | float, 0–1 continuous |
| `Risk_Classification` | `RISK_CLS_TXT` | text |
| `Delay_Probability` | `DELAY_PROB_DEC` | float, 0–1 |
| `Port_Congestion_Level` | `PRT_CNG_LVL` | float, 0–10 |
| `Route_Risk_Index` | `RT_RSK_IDX` | float |

**Security principle:** exposing only a flat, view-based projection means the agent has no
name by which to reference the base table. Combined with the grants in 3.2, `DROP`,
`UPDATE` and `DELETE` are rejected by the engine.

A secondary check in `query_telemetry_db` rejects any statement not beginning with
`SELECT`. This is defence in depth for clearer error messages — it is explicitly **not**
the security boundary, which lives in the grants below.

### 3.2 Role-Based Access Control (RBAC)

Connections use the `USR_FDE_RO` service account:

```sql
GRANT  SELECT ON FDE_VIEWS.VW_ACTIVE_FLEET   TO USR_FDE_RO;
DENY   SELECT ON dbo.TBL_SC_FLEET_HIST_RAW   TO USR_FDE_RO;
DENY   INSERT, UPDATE, DELETE, ALTER ON SCHEMA::dbo TO USR_FDE_RO;
GRANT  INSERT ON FDE_VIEWS.AgentAuditLog     TO USR_FDE_RO;
```

The account cannot read the raw table, cannot modify schema, and cannot write anywhere
except the audit table. The `DENY` on `SCHEMA::dbo` also covers tables added to that schema
in future, so the boundary does not erode as the database grows.

Administrative credentials (`SQL_ADMIN_USERNAME`) are separate, used only by the ingestion
script and the audit viewer, and are never given to the agent.

### 3.3 Auditing

Every decision cycle is written to `FDE_VIEWS.AgentAuditLog`:

| Column | Type | Contents |
|---|---|---|
| `LogID` | `INT IDENTITY` | Primary key |
| `Timestamp` | `DATETIME` | Defaults to `GETDATE()` |
| `SessionID` | `VARCHAR(50)` | Browser thread identifier |
| `NodeExecuted` | `VARCHAR(50)` | `reasoner`, `tools`, or `reasoner_final` |
| `ToolName` | `VARCHAR(100)` | Tool invoked, or `LLM Text Synthesis` |
| `Content` | `NVARCHAR(MAX)` | JSON arguments, raw tool output, or final answer |

One row is written per tool call, per tool result, and per final synthesis, giving a
complete replay of how any answer was produced.

**Scope of the guarantee:** the log is append-only **with respect to the agent identity** —
`USR_FDE_RO` holds `INSERT` and nothing else, so the agent cannot alter or erase its own
trail. It is not cryptographically immutable: an administrator retains full rights over the
table. Achieving true immutability would require a ledger table, append-only storage, or
off-box log shipping. This is a deliberate scope decision, not an oversight.

---

## 4. Retrieval Layer (LLD)

### 4.1 Document Ingestion Pipeline

`scripts/ingest_sop_pinecone.py` maintains the SOP index incrementally:

- **Change detection** — Each source file is MD5-hashed against a cache. Unchanged files
  are skipped entirely.
- **Deterministic chunk identity** — Chunks are keyed `{filename}-chunk-{n}`, so a modified
  document is deleted and re-upserted cleanly rather than accumulating orphans.
- **Deletion propagation** — Files removed from `data/policy/` are purged from the index.
- **Format coverage** — Markdown (header-aware splitting), plain text, PDF, CSV and XLSX.
- **Dimension self-healing** — An index found at the wrong dimensionality is dropped and
  recreated before ingestion proceeds.

### 4.2 Index Isolation

Hosted and local embeddings write to separate indexes (`fde-sop-index-openai` at 1536
dimensions, `fde-sop-index-local` at 1024). Because the vector spaces are incompatible,
isolating them means switching providers can never silently corrupt an existing index.

---

## 5. Presentation Tier

### 5.1 Workspaces

| Workspace | Purpose |
|---|---|
| Dispatch console | Conversational interface, onboarding, live reasoning trace |
| Fleet overview | KPI tiles, geospatial scatter, underlying records |
| Security & audit | Admin-gated reader over the audit log |

Reasoning is rendered transparently: each tool call's generated arguments and raw output
are shown in expandable panels as they occur, and are retained under a collapsed panel
beneath the answer in replayed history. Each answer carries badges for elapsed time, tool
call count, and which tools fired.

### 5.2 Output Contract

The system prompt (`src/prompts/system_prompt.txt`) constrains every final answer to a
fixed structure:

1. **Executive Summary** — the anomaly and the immediate operational risk
2. **Telemetry & Environment Analysis** — a table of readings, plus a causal reading of
   whether the environment or the equipment is at fault
3. **Required Action Plan** — numbered steps derived from the SOP, with an explicit
   citation of the document and rule driving them

This is what converts raw tool output into something a dispatcher can act on without
interpreting JSON.

---

## 6. Deployment Strategy

### 6.1 Infrastructure Topology

| Node | Contents |
|---|---|
| App node (EC2, Ubuntu) | Streamlit under a `systemd` unit with `Restart=always` |
| Database node (EC2, Ubuntu) | MSSQL 2022 in Docker, volume-backed at `/var/opt/mssql` |
| Managed services | Pinecone serverless (AWS `us-east-1`); the configured LLM provider |

The ODBC Driver 18 for SQL Server and `unixodbc-dev` are prerequisites on the app node.

### 6.2 Network Perimeter Security

1. **Ingress (public)** — Only port 8501 on the app node is publicly reachable.
2. **Internal** — Port 1433 is restricted by security group rule to the private IP of the
   app node. The database is not addressable from the public internet.

---

## 7. Known Limitations

Stated explicitly so the design is not read as claiming more than it delivers.

| Area | Current state |
|---|---|
| **Self-correction** | Not implemented. A failed query returns an error string; the model may retry of its own accord, but no retry loop is coded. |
| **Clarification** | No dedicated clarification node. An ambiguous request is answered on the model's own assumptions. |
| **Human-in-the-loop** | No approval or interrupt step. The agent is read-only and advisory, so it recommends escalations rather than performing them — but a reviewer should not infer a gated workflow exists. |
| **Audit immutability** | Append-only for the agent identity, not tamper-proof against an administrator. See 3.3. |
| **Dataset realism** | The dataset is historical (2021-01-01 to 2024-08-29), not a live feed, and its GPS coordinates are synthetic — generated uniformly within a lat 30–50 / lon -120 to -70 box and clipped to its edges. Roughly one point in eight falls over open water. Distances and routes are therefore not physically meaningful. |
| **Semantic naming** | `Cargo_Condition_Code` is a continuous 0–1 score despite the `Code` suffix, which may lead the model to treat it as categorical. |
| **Scale** | Dashboard queries are capped at a 500-row snapshot with a 5-minute cache. No pagination or incremental refresh. |
