# Cold-Chain Logistics AI Assistant

A conversational agent that lets a logistics dispatcher ask plain-English questions about
their fleet and get back a structured incident report with a cited action plan.

Ask *"Find active shipments near Los Angeles, check the weather there, and tell me if
cargo temperature violates the SOP for fresh perishables"* and the agent decides on its
own to query the fleet database, call a live weather API, retrieve the relevant SOP
clause, and synthesise the three into an executive summary with next steps.

The interesting part is not the model — it is the enterprise plumbing around it. This
project is built as a **Forward Deployed Engineer exercise**: take a realistically awful
legacy database, wrap it so an LLM cannot hurt it, ground the answers in company policy,
and make every decision auditable after the fact.

---

## Architecture

```
                        Dispatcher (Streamlit UI)
                                  │
                                  ▼
                   LangGraph Orchestrator  ◄──────┐
                   (reasoner node)                │ loop until
                                  │               │ no more tool calls
                                  ▼               │
                             ToolNode ────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          ▼                       ▼                       ▼
  query_telemetry_db    fetch_corridor_conditions   search_compliance_sop
          │                       │                       │
          ▼                       ▼                       ▼
   MSSQL (Docker)           open-meteo.com           Pinecone index
   FDE_VIEWS.VW_ACTIVE_     live weather for         chunked SOP
   FLEET — read-only        GPS coordinates          documents
   semantic view
          │
          └──────────────► FDE_VIEWS.AgentAuditLog
                           every tool call, argument set,
                           raw result and final answer
```

The agent graph is deliberately minimal — a reasoner node, a prebuilt `ToolNode`, and
`tools_condition` routing between them until the model stops requesting tools.
`MemorySaver` checkpointing gives each browser session its own conversation thread.

---

## The security model

This is the core of the project. An LLM writing SQL against a production database is a
liability, so the database is configured such that a hallucinated destructive query is
*rejected by the server*, not merely discouraged by a prompt.

| Layer | Mechanism |
|---|---|
| **Semantic view** | `FDE_VIEWS.VW_ACTIVE_FLEET` renames legacy columns (`IOT_TEMP_VAL_C` → `Current_Temperature_C`) and casts types, so the model reasons over readable names without a schema dictionary |
| **Least privilege** | `USR_FDE_RO` is granted `SELECT` on that view and nothing else |
| **Explicit denial** | `DENY SELECT` on the raw table; `DENY INSERT, UPDATE, DELETE, ALTER` on the whole `dbo` schema |
| **Narrow write exception** | A single `GRANT INSERT` on `FDE_VIEWS.AgentAuditLog` so the agent can log its own activity |
| **Defense in depth** | `query_telemetry_db` also rejects any statement not starting with `SELECT` ([src/agent_tools.py](src/agent_tools.py)) — a second line, not the actual guardrail |

The application layer cannot grant itself more than this, because the permissions live in
the database. Verify the boundary yourself with `scripts/verification_queries.sql`.

---

## Quickstart

**Prerequisites:** Docker, Python 3.12, and the
[ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server).

```bash
# 1. Spin up the legacy database
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=<your-sa-password>" \
  -p 1433:1433 --name legacy-mssql \
  -d mcr.microsoft.com/mssql/server:2022-latest

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env    # then fill in the values (see table below)

# 4. Load the legacy data (~32k rows)
python scripts/ingest_legacy_data.py

# 5. Index the SOP documents into Pinecone
python scripts/ingest_sop_pinecone.py
```

Then run these two SQL scripts against the **admin** connection, in order:

1. `scripts/setup_security_and_view.sql` — semantic view + read-only agent role
2. `scripts/setup_audit_log.sql` — audit table + the single INSERT grant

```bash
# 6. Launch the console
streamlit run src/ui.py
```

`docs/instructions.md` is the full step-by-step runbook, including EC2 provisioning, the
VS Code SQL connection profiles, and systemd deployment.

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `PINECONE_API_KEY` | **yes** | Vector store for SOP retrieval |
| `SQL_SERVER_HOST` | **yes** | `localhost`, or the EC2 public IP |
| `SQL_SERVER_PORT` | **yes** | `1433` |
| `SQL_ADMIN_USERNAME` | **yes** | `sa` — used by the ingestion script and the audit-log viewer |
| `SQL_ADMIN_PASSWORD` | **yes** | Must match `MSSQL_SA_PASSWORD` from the `docker run` |
| `SQL_AGENT_USER` | **yes** | `USR_FDE_RO` — the restricted identity the agent runs as |
| `SQL_AGENT_PASSWORD` | **yes** | Must match the password in `setup_security_and_view.sql` |
| `Agent_llm` | no | `OPENAI` \| `DEEPSEEK` \| anything else → local Ollama |
| `Embeddings_model` | no | `OPENAI` (1536-dim) \| `LOCAL` (1024-dim, default) |
| `Local_Embedding_Model` | no | HuggingFace model id, default `BAAI/bge-m3` |
| `DEEPSEEK_API_KEY` | if `DEEPSEEK` | |
| `OPENAI_API_KEY` | if `OPENAI` | Read implicitly by the LangChain OpenAI classes |

**Provider switching.** Both the reasoner and the embeddings are swappable by env var, so
the whole stack runs free and offline (Ollama + BGE-M3) or fully hosted. Each embedding
provider writes to its **own** Pinecone index (`fde-sop-index-openai` vs
`fde-sop-index-local`) because their vector dimensions differ — switching providers can
never corrupt an existing index.

---

## Try it

Two questions that exercise opposite behaviours:

**1. Multi-hop chaining** — requires three dependent tool calls in sequence:

> Find any active shipments near Los Angeles (Latitude ~33.8, Longitude ~-118.1).
> Check the local weather there, and tell me if the current cargo temperature
> violates the SOP for fresh perishables.

**2. Restraint** — the answer lives only in the SOP, so a well-behaved agent makes *one*
retrieval call and skips the database and weather API entirely:

> I'm a new dispatcher on the night shift. Can you quickly explain the difference
> between a Tier 1 and Tier 2 escalation?

Knowing when *not* to call a tool is as important as chaining them correctly.

The UI renders each tool's generated arguments and raw output in expandable panels, so
the reasoning is inspectable rather than a black box. The **🛡️ Security & Audit Logs**
tab shows the same trace persisted in SQL.

---

## Project layout

```
├── data/
│   ├── policy/          SOP markdown — source documents for the vector index
│   ├── raw/             Kaggle logistics dataset (32k rows)
│   ├── source/          Dataset provenance
│   └── cache/           MD5 hashes enabling incremental re-indexing
├── docs/
│   └── instructions.md  Full operational runbook, phase by phase
├── scripts/
│   ├── ingest_legacy_data.py       CSV → deliberately "legacy" MSSQL schema
│   ├── ingest_sop_pinecone.py      Incremental multi-format document indexer
│   ├── setup_security_and_view.sql Semantic view + least-privilege role
│   ├── setup_audit_log.sql         Audit table + narrow INSERT grant
│   └── verification_queries.sql    Per-phase checks, including the deny tests
├── src/
│   ├── agent_tools.py   The three tools, plus provider routing
│   ├── orchestrator.py  LangGraph state machine
│   ├── ui.py            Streamlit console + audit viewer
│   └── prompts/         System prompt enforcing the report format
└── Misc/Materials/      Business presentation and technical design document
```

### Notable implementation details

**Incremental vector ingestion.** `ingest_sop_pinecone.py` hashes each policy file and
skips unchanged ones. Changed files are deleted from the index by deterministic chunk ID
and re-upserted; files removed from `data/policy/` are purged from Pinecone entirely. It
parses `.md` (header-aware), `.txt`, `.pdf`, `.csv` and `.xlsx`, and self-heals an index
created at the wrong dimension.

**Structured output contract.** [src/prompts/system_prompt.txt](src/prompts/system_prompt.txt)
forces every answer into Executive Summary → Telemetry table → Action Plan with an SOP
citation, which is what turns raw tool output into something a dispatcher can act on.

---

## Data source

[Logistics and Supply Chain Dataset](https://www.kaggle.com/datasets/datasetengineer/logistics-and-supply-chain-dataset/data)
(Kaggle). The ingestion script intentionally *degrades* the clean column names into a
2000s-era enterprise schema — a realistic starting condition, since an FDE never
inherits tidy tables.
