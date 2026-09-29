import os
import sys
import uuid
import json
import time
import urllib
from pathlib import Path
from dotenv import load_dotenv
import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from langchain_core.messages import HumanMessage, ToolMessage

# ==========================================
# 1. PATH & ENVIRONMENT RESOLUTION
# ==========================================
script_dir = Path(__file__).resolve().parent  # points to src/
project_root = script_dir.parent              # climbs to project root

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

load_dotenv(project_root / ".env")

# ==========================================
# 2. PAGE CONFIG
# ==========================================
# Set before the orchestrator import below, which loads embedding models and is slow.
# Fast UI first means the page chrome paints while the heavy import is still running.
st.set_page_config(
    page_title="Cold Chain Control Tower",
    page_icon=":material/ac_unit:",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "About": (
            "**Cold Chain Control Tower**\n\n"
            "An AI dispatch assistant for cold-chain logistics incidents. Converts plain "
            "English into SQL over a read-only telemetry view, checks live corridor "
            "conditions, and grounds every action plan in the company's Standard "
            "Operating Procedures.\n\n"
            "Built by **Ujjwal Khanna**."
        ),
        "Report a bug": "https://github.com/UjjwalK08/supply-chain-logisitics-FDE-project/issues",
    },
)

from src.orchestrator import fde_agent  # noqa: E402  (must follow set_page_config)

# ==========================================
# 3. SQL CREDENTIALS & SOP CONSTANTS
# ==========================================
db_host = os.getenv("SQL_SERVER_HOST", "localhost")
db_port = os.getenv("SQL_SERVER_PORT", "1433")
db_user = os.getenv("SQL_AGENT_USER", "USR_FDE_RO")
db_password = os.getenv("SQL_AGENT_PASSWORD")

# Thresholds mirrored from data/policy/Cold_Chain_Incident_SOP_v2.md. Kept here as
# named constants so the dashboard and the SOP the agent cites cannot drift apart.
SOP_MAX_TEMP_C = 4.0        # Fresh perishables ceiling; above this is a breach
SOP_MIN_TEMP_C = 0.0        # Fresh perishables floor
SOP_PORT_CONGESTION = 7.0   # Standard routing suspended above this index
SOP_DELAY_PROBABILITY = 0.65  # High Risk + above this -> Tier 2 escalation

AUTHOR = "Ujjwal Khanna"
REPO_URL = "https://github.com/UjjwalK08/supply-chain-logisitics-FDE-project"

# Engine for the agent to write logs using its restricted credentials
connection_string = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={db_host},{db_port};"
    f"DATABASE=master;"
    f"UID={db_user};"
    f"PWD={db_password};"
    f"Encrypt=no;"
    f"TrustServerCertificate=yes;"
)

log_params = urllib.parse.quote_plus(connection_string)

log_engine = create_engine(f"mssql+pyodbc:///?odbc_connect={log_params}")

def write_audit_log(session_id, node_name, tool_name, content):
    """Silently writes agent execution traces to the SQL audit table using agent permissions."""
    try:
        with log_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO FDE_VIEWS.AgentAuditLog (SessionID, NodeExecuted, ToolName, Content)
                VALUES (:session_id, :node_name, :tool_name, :content)
            """), {
                "session_id": session_id,
                "node_name": node_name,
                "tool_name": tool_name,
                "content": content
            })
            conn.commit()
    except Exception as e:
        print(f"Audit Log Failed (Silent): {e}")

# ==========================================
# 4. FLEET SNAPSHOT
# ==========================================
@st.cache_data(ttl=300, show_spinner=False)
def load_fleet_snapshot(limit: int = 500) -> tuple[pd.DataFrame, str]:
    """
    Most recent `limit` rows from the read-only semantic view.

    The underlying dataset is historical (2021-2024), not a live feed, so this is
    deliberately framed as a point-in-time snapshot rather than current status.
    Returns (frame, error). On failure the frame is empty and `error` carries the
    reason, so callers can say what actually broke instead of guessing.
    """
    # `limit` is an internal constant, never user input, and is coerced to int here.
    # Inlined rather than bound because a parameterised TOP is driver-dependent, and a
    # binding failure would surface as a misleading "database unreachable".
    safe_limit = max(1, int(limit))
    query = text(
        f"SELECT TOP {safe_limit} [Timestamp], Latitude, Longitude, Current_Temperature_C, "
        "Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index "
        "FROM FDE_VIEWS.VW_ACTIVE_FLEET "
        "ORDER BY [Timestamp] DESC"
    )
    try:
        with log_engine.connect() as conn:
            return pd.read_sql(query, conn), ""
    except Exception as e:
        print(f"Fleet snapshot unavailable (Silent): {e}")
        return pd.DataFrame(), str(e)

def render_fleet_metrics(df: pd.DataFrame) -> None:
    """Four KPI tiles derived from the snapshot. No-op when the database is down."""
    if df.empty:
        return

    breaches = int((df["Current_Temperature_C"] > SOP_MAX_TEMP_C).sum())
    avg_temp = float(df["Current_Temperature_C"].mean())
    high_risk = int((df["Risk_Classification"] == "High Risk").sum())
    breach_pct = 100 * breaches / len(df)

    cols = st.columns(4)
    cols[0].metric(
        "Shipments in snapshot", f"{len(df):,}",
        icon=":material/local_shipping:",
        help="Most recent records in the fleet view",
    )
    cols[1].metric(
        "Cold-chain breaches", f"{breaches:,}",
        delta=f"{breach_pct:.0f}% of fleet",
        delta_color="inverse",
        icon=":material/thermostat:",
        help=f"Cargo temperature above the {SOP_MAX_TEMP_C}°C SOP ceiling",
    )
    cols[2].metric(
        "Average cargo temp", f"{avg_temp:.1f}°C",
        icon=":material/device_thermostat:",
        help=f"SOP range for fresh perishables: {SOP_MIN_TEMP_C}–{SOP_MAX_TEMP_C}°C",
    )
    cols[3].metric(
        "High risk shipments", f"{high_risk:,}",
        icon=":material/warning:",
        help="Risk classification reported as High Risk",
    )

    as_of = pd.to_datetime(df["Timestamp"]).max()
    st.caption(
        f"Snapshot as of {as_of:%d %b %Y}. This dataset is historical, not a live feed."
    )

# ==========================================
# 5. AGENT TURN EXECUTION & THREAD RECOVERY
# ==========================================
# A LangGraph checkpoint can be left holding an assistant message that requested tools
# but no matching tool results, if a run is interrupted between the reasoner and the
# ToolNode (Streamlit hot-reload, browser refresh, a tool raising). Replaying that
# history makes any OpenAI-compatible API reject the entire request, so the thread is
# permanently unusable until it is rotated.
_DANGLING_TOOL_CALL_MARKERS = (
    "must be followed by tool messages",
    "insufficient tool messages",
    "tool_calls must be followed by",
)

def is_dangling_tool_call_error(exc: Exception) -> bool:
    """True if this exception is the unreplayable-history rejection described above."""
    text_ = str(exc).lower()
    return any(marker in text_ for marker in _DANGLING_TOOL_CALL_MARKERS)

def run_agent_turn(user_input: str, config: dict):
    """Streams one turn of the graph, rendering live traces. Returns (response, traces)."""
    final_response = ""
    traces = []
    session_id = config["configurable"]["thread_id"]

    with st.status("Initialising core reasoner node", expanded=True) as status:
        events = fde_agent.stream(
            {"messages": [HumanMessage(content=user_input)]},
            config=config,
            stream_mode="updates"
        )

        for event in events:
            for node_name, node_state in event.items():

                if node_name == "reasoner":
                    latest_msg = node_state["messages"][-1]

                    # A. Intercept Tool Call Requests (Inputs)
                    if getattr(latest_msg, "tool_calls", None):
                        status.update(label="Agent generated tool parameters")
                        for tool_call in latest_msg.tool_calls:
                            st.markdown(f"**Intent recognised:** `{tool_call['name']}`")
                            with st.expander(f"Generated input — {tool_call['name']}",
                                             icon=":material/input:"):
                                st.json(tool_call['args'])

                            traces.append({
                                "type": "tool_input",
                                "name": tool_call['name'],
                                "args": tool_call['args']
                            })

                            write_audit_log(
                                session_id=session_id,
                                node_name="reasoner",
                                tool_name=tool_call['name'],
                                content=json.dumps(tool_call['args'])
                            )

                    # B. Intercept Final Generation
                    if latest_msg.content:
                        final_response = latest_msg.content
                        status.update(label="Generating operational resolution report")

                        write_audit_log(
                            session_id=session_id,
                            node_name="reasoner_final",
                            tool_name="LLM Text Synthesis",
                            content=final_response
                        )

                elif node_name == "tools":
                    status.update(label="Executing enterprise subsystem tools")
                    for msg in node_state.get("messages", []):
                        if isinstance(msg, ToolMessage):
                            with st.expander(f"Raw output — {msg.name}",
                                             icon=":material/output:"):
                                st.code(msg.content, language="text")

                            traces.append({
                                "type": "tool_output",
                                "name": msg.name,
                                "content": msg.content
                            })

                            write_audit_log(
                                session_id=session_id,
                                node_name="tools",
                                tool_name=msg.name,
                                content=msg.content
                            )

        status.update(label="Incident evaluation complete", state="complete", expanded=False)

    return final_response, traces

def render_run_badges(meta: dict) -> None:
    """Elapsed time, tool-call count and which tools fired, under an answer."""
    if not meta:
        return
    with st.container(horizontal=True, gap="small"):
        st.badge(f"{meta['elapsed']:.1f}s", icon=":material/timer:", color="gray")
        st.badge(
            f"{meta['tool_calls']} tool call{'s' if meta['tool_calls'] != 1 else ''}",
            icon=":material/build:",
            color="blue" if meta["tool_calls"] else "gray",
        )
        for tool_name in meta.get("tools_used", []):
            st.badge(tool_name, icon=":material/check:", color="green")

def render_trace_expander(traces: list) -> None:
    """Replayed history: evidence grouped and collapsed *below* the answer."""
    if not traces:
        return
    with st.expander("How I Got This", icon=":material/manage_search:"):
        for trace in traces:
            if trace["type"] == "tool_input":
                st.markdown(f"**Called** `{trace['name']}` with:")
                st.json(trace["args"])
            else:
                st.markdown(f"**Returned from** `{trace['name']}`:")
                st.code(trace["content"], language="text")

# ==========================================
# 6. SESSION STATE
# ==========================================
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "ui_messages" not in st.session_state:
    st.session_state.ui_messages = []

if "pending_prompt" not in st.session_state:
    st.session_state.pending_prompt = None

thread_config = {"configurable": {"thread_id": st.session_state.thread_id}}

STARTER_PROMPTS = {
    "Cold-chain breach near LA": (
        "Find any active shipments near Los Angeles (Latitude ~33.8, Longitude ~-118.1). "
        "Check the local weather there, and tell me if the current cargo temperature "
        "violates the SOP for fresh perishables."
    ),
    "Tier 1 vs Tier 2 escalation": (
        "I'm a new dispatcher on the night shift. Can you quickly explain the difference "
        "between a Tier 1 and Tier 2 escalation?"
    ),
    "Port congestion protocol": (
        "Port congestion at Long Beach is climbing. What does the SOP require me to do, "
        "and which shipments are currently affected?"
    ),
    "Worst cargo temperatures": (
        "Which shipments currently have the worst cargo temperatures, and what is the "
        "required mitigation for each under the SOP?"
    ),
}

# ==========================================
# 7. SIDEBAR
# ==========================================
with st.sidebar:
    st.space("medium")  # drops the whole block below the window chrome
    st.markdown("### :material/ac_unit: Cold Chain Control Tower")
    st.caption("Incident intelligence for refrigerated freight")
    st.space("medium")  # separates the heading from the navigation below it

    app_mode = st.segmented_control(
        "Workspace",
        options=["Dispatch console", "Fleet overview", "Security & audit"],
        default="Dispatch console",
        label_visibility="collapsed",
        width="stretch",
    )
    # A segmented control can return None if the user deselects the active option.
    if app_mode is None:
        app_mode = "Dispatch console"

    if st.button("Start a new session", icon=":material/refresh:", width="stretch"):
        st.session_state.ui_messages = []
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.pending_prompt = None
        st.rerun()

    st.caption(f"Reasoning architecture · `{os.getenv('Agent_llm', 'DEEPSEEK')}`")
    st.caption(f"Session token · `{st.session_state.thread_id[:8]}`")

    st.markdown(f"**Built by {AUTHOR}**")
    st.caption(f"[View the source]({REPO_URL})")

# ==========================================
# 8. DISPATCH CONSOLE
# ==========================================
if app_mode == "Dispatch console":
    st.title("Cold Chain Control Tower")
    st.caption(
        "Ask in plain English — the agent writes its own SQL, checks live corridor "
        "conditions, and cites the SOP clause behind every action it recommends."
    )

    # ---- Onboarding, shown only before the first question ----
    if not st.session_state.ui_messages:
        snapshot, _snapshot_error = load_fleet_snapshot()
        render_fleet_metrics(snapshot)

        with st.container(horizontal=True, gap="medium"):
            with st.container(border=True):
                st.markdown(":material/database: **Text-to-SQL telemetry**")
                st.caption(
                    "Translates your question into T-SQL and runs it against a read-only "
                    "view — GPS position, cargo temperature, risk classification and "
                    "delay probability. No SQL knowledge needed."
                )
            with st.container(border=True):
                st.markdown(":material/cloud: **Corridor Conditions**")
                st.caption(
                    "Pulls live weather and wind for any coordinates from a public API, "
                    "then derives a transit disruption index."
                )
            with st.container(border=True):
                st.markdown(":material/gavel: **Compliance SOPs**")

                st.caption(
                    "Retrieves the governing clause from the company's Standard "
                    "Operating Procedures so every action plan is cited."
                )

        st.caption(
            f"**Thresholds it enforces** — fresh perishables must stay between "
            f"{SOP_MIN_TEMP_C}°C and {SOP_MAX_TEMP_C}°C · standard routing is suspended "
            f"above a port congestion index of {SOP_PORT_CONGESTION} · High Risk cargo "
            f"with delay probability over {SOP_DELAY_PROBABILITY} escalates to Tier 2."
        )

        with st.expander("How This Works", icon=":material/schema:"):
            st.markdown(
                """
**Architecture.** A LangGraph state machine loops between a reasoning node and a tool
node until the model stops requesting tools. The model decides for itself which of the
three tools to call and in what order — a question answerable from the SOP alone costs
one retrieval, while an incident triage chains all three.

**Why the database is safe.** The agent connects as `USR_FDE_RO`, a login granted
`SELECT` on a single semantic view and nothing else. The raw legacy table is explicitly
denied, as are all writes to the `dbo` schema. A hallucinated `DROP TABLE` is rejected
by the server, not merely discouraged by a prompt. The one exception is `INSERT` on the
audit table, so the agent can record its own activity.

**Why the columns read cleanly.** The view renames a deliberately awful legacy schema
(`IOT_TEMP_VAL_C`, `RT_RSK_IDX`) into readable names, so the model reasons over English
rather than needing a schema dictionary.

**Auditability.** Every tool call, argument set, raw result and final answer is written
to `FDE_VIEWS.AgentAuditLog` and is inspectable under *Security & audit*.
                """
            )

    # ---- Replay conversation history ----
    for entry in st.session_state.ui_messages:
        with st.chat_message(entry["role"], avatar=":material/person:"
                             if entry["role"] == "user" else ":material/ac_unit:"):
            if entry["role"] == "assistant":
                with st.container(border=True):
                    st.markdown(entry["content"])
                render_run_badges(entry.get("meta"))
                render_trace_expander(entry.get("traces", []))
            else:
                st.markdown(entry["content"])

    # ---- Starter chips, shown until the conversation begins ----
    if not st.session_state.ui_messages:
        chosen = st.pills(
            "Try one of these",
            options=list(STARTER_PROMPTS),
            selection_mode="single",
            default=None,
            key="starter_pills",
        )
        if chosen:
            st.session_state.pending_prompt = STARTER_PROMPTS[chosen]
            # Clearing the selection is required, not cosmetic: leaving it set would
            # re-fire the same prompt on the next rerun and loop indefinitely.
            if "starter_pills" in st.session_state:
                del st.session_state["starter_pills"]
            st.rerun()

    typed = st.chat_input(
        "Query fleet telemetry, corridor updates, or compliance thresholds",
        submit_mode="disable",  # blocks mid-run submits, which is what corrupts a thread
    )

    user_input = typed or st.session_state.pending_prompt
    st.session_state.pending_prompt = None

    if user_input:
        st.session_state.ui_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(user_input)

        with st.chat_message("assistant", avatar=":material/ac_unit:"):
            final_response = ""
            current_traces = []
            started = time.perf_counter()

            try:
                final_response, current_traces = run_agent_turn(user_input, thread_config)

            except Exception as exc:
                if is_dangling_tool_call_error(exc):
                    # Rotate onto a clean thread and retry once. The question is
                    # self-contained, so only prior conversation context is lost.
                    st.warning(
                        "This conversation thread was interrupted mid-tool-call and can no "
                        "longer be replayed. Starting a fresh thread and retrying — earlier "
                        "turns in this conversation will not be carried over.",
                        icon=":material/restart_alt:",
                    )
                    st.session_state.thread_id = str(uuid.uuid4())
                    retry_config = {"configurable": {"thread_id": st.session_state.thread_id}}
                    try:
                        final_response, current_traces = run_agent_turn(user_input, retry_config)
                    except Exception as retry_exc:
                        st.error(f"Retry on a fresh thread also failed: {retry_exc}",
                                 icon=":material/error:")
                else:
                    st.error(f"Agent execution failed: {exc}", icon=":material/error:")

            if final_response:
                meta = {
                    "elapsed": time.perf_counter() - started,
                    "tool_calls": sum(1 for t in current_traces if t["type"] == "tool_input"),
                    "tools_used": sorted({
                        t["name"] for t in current_traces if t["type"] == "tool_input"
                    }),
                }
                with st.container(border=True):
                    st.markdown(final_response)
                render_run_badges(meta)

                st.session_state.ui_messages.append({
                    "role": "assistant",
                    "content": final_response,
                    "traces": current_traces,
                    "meta": meta,
                })
            else:
                st.error(
                    "The run finished without producing a report. Try rephrasing, or "
                    "start a new session from the sidebar.",
                    icon=":material/error:",
                )

# ==========================================
# 9. FLEET OVERVIEW
# ==========================================
elif app_mode == "Fleet overview":
    st.title("Fleet Overview")
    st.caption("Point-in-time snapshot of the most recent shipments in the telemetry view.")

    snapshot, snapshot_error = load_fleet_snapshot()

    if snapshot.empty:
        st.warning(
            "The fleet snapshot could not be loaded. Check that the `legacy-mssql` "
            "container is running and that the agent credentials in `.env` are valid.",
            icon=":material/database_off:",
        )
        if snapshot_error:
            with st.expander("Error detail", icon=":material/bug_report:"):
                st.code(snapshot_error, language="text")
    else:
        render_fleet_metrics(snapshot)

        plot_df = snapshot.dropna(subset=["Latitude", "Longitude"]).copy()
        plot_df["In breach"] = plot_df["Current_Temperature_C"] > SOP_MAX_TEMP_C
        # Red above the SOP ceiling, blue within it.
        plot_df["color"] = plot_df["In breach"].map(
            {True: "#F87171", False: "#60A5FA"}
        )

        st.subheader("Shipment Positions")
        st.caption(
            f"Red markers are above the {SOP_MAX_TEMP_C}°C SOP ceiling; blue are within range."
        )
        st.caption(
            ":material/info: Coordinates in this public dataset are synthetic — generated "
            "uniformly inside a lat 30–50 / lon −120 to −70 box, then clipped to its edges. "
            "Around one point in eight therefore falls over open water. The markers are "
            "plotted correctly; the source data simply isn't routed to real roads or ports."
        )
        st.map(plot_df, latitude="Latitude", longitude="Longitude", color="color", size=12000)

        st.subheader("Underlying Records")
        st.dataframe(
            snapshot,
            column_config={
                "Timestamp": st.column_config.DatetimeColumn("Recorded", format="DD/MM/YYYY HH:mm"),
                "Latitude": st.column_config.NumberColumn("Lat", format="%.3f"),
                "Longitude": st.column_config.NumberColumn("Lon", format="%.3f"),
                "Current_Temperature_C": st.column_config.NumberColumn("Cargo °C", format="%.1f"),
                "Risk_Classification": "Risk",
                "Delay_Probability": st.column_config.ProgressColumn(
                    "Delay probability", min_value=0.0, max_value=1.0, format="%.2f"
                ),
                "Port_Congestion_Level": st.column_config.NumberColumn("Port congestion", format="%.1f"),
                "Route_Risk_Index": st.column_config.NumberColumn("Route risk", format="%.2f"),
            },
            hide_index=True,
            width="stretch",
            height=420,
        )

# ==========================================
# 10. SECURITY & AUDIT
# ==========================================
elif app_mode == "Security & audit":
    st.title("Agent Audit Trail")
    st.caption("Every tool call, argument set and generated answer, as recorded in SQL.")

    st.markdown(
        "The agent writes to `FDE_VIEWS.AgentAuditLog` using an `INSERT`-only grant — "
        "the single write exception to its otherwise read-only role. Reading the log "
        "back requires administrative credentials."
    )

    with st.form("admin_auth_form"):
        col1, col2 = st.columns(2)
        with col1:
            input_user = st.text_input("Admin username", value=os.getenv("SQL_ADMIN_USERNAME", ""))
        with col2:
            input_pass = st.text_input("Admin password", type="password", value="")

        submit_admin = st.form_submit_button(
            "Authenticate and load logs", icon=":material/lock_open:", width="stretch"
        )

    if submit_admin:
        expected_admin_user = os.getenv("SQL_ADMIN_USERNAME")
        expected_admin_pass = os.getenv("SQL_ADMIN_PASSWORD")

        if not expected_admin_user or not expected_admin_pass:
            st.error(
                "Admin credentials are not configured. Set `SQL_ADMIN_USERNAME` and "
                "`SQL_ADMIN_PASSWORD` in your `.env` file, then restart the app.",
                icon=":material/settings:",
            )
        elif input_user == expected_admin_user and input_pass == expected_admin_pass:
            try:
                # Build an isolated admin connection string for viewing data
                admin_params = urllib.parse.quote_plus(
                    "DRIVER={ODBC Driver 18 for SQL Server};"
                    f"SERVER={db_host},{db_port};"
                    "DATABASE=master;"
                    f"UID={input_user};"
                    f"PWD={input_pass};"
                    "Encrypt=no;"
                    "TrustServerCertificate=yes;"
                )
                admin_engine = create_engine(f"mssql+pyodbc:///?odbc_connect={admin_params}")

                with admin_engine.connect() as conn:
                    query = """
                        SELECT LogID, Timestamp, SessionID, NodeExecuted, ToolName, Content
                        FROM FDE_VIEWS.AgentAuditLog
                        ORDER BY Timestamp DESC
                    """
                    df = pd.read_sql(query, conn)

                st.success("Authenticated as administrator.", icon=":material/verified_user:")

                if not df.empty:
                    st.caption(
                        f"{len(df):,} entries across "
                        f"{df['SessionID'].nunique():,} session(s)."
                    )
                    st.dataframe(
                        df,
                        column_config={
                            "LogID": st.column_config.NumberColumn("ID", format="%d"),
                            "Timestamp": st.column_config.DatetimeColumn(
                                "Execution time", format="DD/MM/YYYY HH:mm:ss"
                            ),
                            "SessionID": "Session token",
                            "NodeExecuted": "Graph node",
                            "ToolName": "Tool triggered",
                            "Content": "Raw payload",
                        },
                        hide_index=True,
                        width="stretch",
                        height=600,
                    )
                else:
                    st.info(
                        "No audit entries yet. Ask a question in the dispatch console — "
                        "rows are written as the agent runs.",
                        icon=":material/inbox:",
                    )

            except Exception as e:
                st.error(f"Database query failed: {e}", icon=":material/error:")
        else:
            st.error("Invalid administrator credentials.", icon=":material/lock:")
