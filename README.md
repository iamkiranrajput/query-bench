# SQL Query Assistant

**Natural-language SQL powered by OpenAI Codex OAuth + Model Context Protocol (MCP)**

Ask questions about any relational database in plain English. An OpenAI Codex
model drives a set of **MCP tools** that explore the schema, build and
validate SQL, and execute it safely — then explains the results.

## OpenAI Build Week setup

1. Start the backend and Angular UI using the existing quick-start steps.
2. Connect your database in **Settings**.
3. Open **OpenAI Codex** and select the settings icon.
4. Select **Sign in with OpenAI**, approve the device code in the browser, and
   QueryBench will dynamically load the Codex models available to that account.
5. Open **Database Context** and upload database context such as Markdown
   schema notes, a CSV data dictionary, JSON/YAML business rules, or example
   SQL. The file is scoped to the connected database and grounds the next turn.

OpenAI OAuth credentials use encrypted-at-rest storage (Windows DPAPI, or AES-GCM
with `COPILOT_TOKEN_ENC_KEY` on other platforms). Uploaded context is stored
locally under `server/data/user_contexts/`, excluded from Git, and treated as
guidance; live schema inspection and query results always take precedence.

## Demo Video

[Watch the 10-minute QueryBench hackathon demo on youtube](https://youtu.be/bP9VPO2PLC4)


[Watch the 4-minute QueryBench hackathon demo](https://youtu.be/bP9VPO2PLC4)

This walkthrough shows the agent experience end to end: database context files,
MCP tool calls, PostGIS/pgvector-aware SQL,
validation, execution, and the trust panel that explains why the answer can be
trusted.

---

## How It Works

Instead of asking a model to blurt out SQL in one shot, this project runs an
**agent loop**: the Copilot model is given a toolbox (MCP tools) and decides,
step by step, how to discover the schema, assemble a query, validate it, and
run it.

```
┌──────────────────────────────────────────────────────────────────┐
│                      Angular Frontend (UI)                         │
│   ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐      │
│   │  Copilot   │ │   Schema   │ │ Dashboard  │ │    Data    │      │
│   │    Chat    │ │  Explorer  │ │  (Logs)    │ │  Analytics │      │
│   └────────────┘ └────────────┘ └────────────┘ └────────────┘      │
│                        HTTP REST API                               │
└───────────────────────────────┬────────────────────────────────────┘
                                 │
┌───────────────────────────────▼────────────────────────────────────┐
│                       FastAPI Backend (Python)                       │
│                                                                      │
│   ┌───────────────── OpenAI Codex Agent Loop ──────────────────┐   │
│   │  user question                                               │   │
│   │      │                                                       │   │
│   │      ▼   model picks tools ──►  MCP tool calls  ──┐          │   │
│   │   reason ◄──────────── tool results ◄─────────────┘          │   │
│   │      │   (repeat until the answer is ready)                  │   │
│   │      ▼                                                       │   │
│   │   final answer + SQL + rows                                  │   │
│   └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│   │  MCP Server  │  │ Schema Index │  │  Connection / Query Mgmt  │ │
│   │  (MCP tools) │  │  (optional   │  │  (SQLAlchemy pools)       │ │
│   │              │  │   FAISS RAG) │  │                           │ │
│   └──────────────┘  └──────────────┘  └───────────────────────────┘ │
└───────────────────────────────┬────────────────────────────────────┘
                                 │
┌───────────────────────────────▼────────────────────────────────────┐
│      Your Database  ·  PostgreSQL / MySQL / SQL Server / Oracle      │
└──────────────────────────────────────────────────────────────────────┘
```

The same MCP tool surface is also exposed over **stdio** and (optionally) **HTTP**,
so IDE clients like VS Code, Cursor, or Claude Desktop can use it directly.

---

## Database Context Files

Upload Markdown, text, JSON, YAML, CSV, or SQL files containing schema notes,
business rules, metric definitions, data dictionaries, and example queries.
Files are stored locally, scoped to the selected database, and automatically
included in Codex conversations. Live schema inspection remains authoritative.

---

## MCP Tools

The agent has access to tools across a few categories:

| Category | Tools |
|----------|-------|
| **Discovery** | `search_tables`, `search_columns`, `introspect_schema`, `preview_data`, `sample_column_values` |
| **Relationships** | `check_relationships`, `discover_join_paths` |
| **Advanced SQL** | `detect_extensions`, `semantic_data_search` (pgvector) |
| **SQL lifecycle** | `generate_sql`, `validate_sql`, `execute_sql`, `explain_sql`, `fix_sql` |
| **Connection** | `connect_database`, `switch_database`, `get_connection_profile`, `analyze_connection_performance`, `validate_server_compatibility`, `check_db_integrity` |

All execution is **SELECT-only** and passes through a SQL validator (injection
detection, keyword blocking, single-statement enforcement) before it runs.

---

## Quick Start

### Prerequisites
- **Python 3.10+** (3.13 supported)
- **Node.js 18+** and npm (Angular 17 requires ≥ 18)
- A reachable **SQL database** (PostgreSQL, MySQL, SQL Server, or Oracle)
- An **OpenAI account with Codex access** (authenticated at runtime via OAuth device code)

### Backend Setup
```bash
cd server

# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows PowerShell
source venv/bin/activate      # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — generate a SECRET_KEY:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

# Start server
python main.py
```
Backend: `http://localhost:2222` · API docs: `http://localhost:2222/api/docs`

### Frontend Setup
```bash
cd ui
npm install
npm start
```
Frontend: `http://localhost:1111`

> Override ports via `PORT=` in `server/.env` and the `--port` flag in the
> `start` script of `ui/package.json`.

### Usage
1. Open `http://localhost:1111`.
2. Go to **Settings → Database Connections** and connect to your database.
3. Open **OpenAI Codex**, sign in with your OpenAI account (device-code prompt), and
   ask questions in natural language.
4. Use **Schema Explorer**, **Dashboard**, and **Analytics** to browse the
   schema and review query history.

---

## Demo database (PostGIS + pgvector)

A ready-to-run demo database showcases the spatial + semantic features. It seeds
a sizeable geospatial retail dataset — **100 stores, 5,000 customers, 30,000
orders, 24 products** (all SRID 4326).

**Option A — Docker (self-contained):**

```bash
cd demo
$env:POSTGRES_PASSWORD = "<choose-a-strong-password>"   # PowerShell
docker compose up --build -d
# populate product embeddings for pgvector semantic search:
$env:DEMO_DB_PASSWORD = $env:POSTGRES_PASSWORD
python seed_embeddings.py
```

Connect Query Bench to `localhost:5433` / `querybench_demo` / `querybench`.

**Option B — No Docker (hosted or local PostgreSQL):** run the single combined
script [`demo/setup_hosted.sql`](demo/setup_hosted.sql) on any PostgreSQL that
has **PostGIS** and **pgvector** available:

- **Hosted (fastest):** create a free **Supabase** project (PostGIS + pgvector
  preinstalled) or an **Azure Database for PostgreSQL Flexible Server** (enable
  `POSTGIS` and `VECTOR` in the `azure.extensions` allow-list), then paste/run
  the script in its SQL editor:
  ```bash
  psql "<connection-string>" -f demo/setup_hosted.sql
  ```
- **Local PostgreSQL:** install PostGIS (via StackBuilder) and pgvector, then
  run the same script.

Then populate embeddings (set `DEMO_DB_HOST/PORT/NAME/USER/PASSWORD` to your DB):

```bash
python demo/seed_embeddings.py
```

> **No PostGIS/pgvector available?** The app still works — `detect_extensions`
> reports them absent and the agent falls back to standard ANSI SQL. The map
> view also renders plain `latitude`/`longitude` columns, so spatial results
> still plot even without PostGIS.

Once connected, try these demos:

- *"What is our net revenue from active customers?"* → uses definitions from
  your uploaded database context, then verifies them against the live schema.
- *"Which stores are within 5 km of downtown?"* → uses uploaded spatial
  conventions and emits correct
  `ST_DWithin(geom::geography, …)` PostGIS SQL.
- *"Find products similar to 'warm clothing for winter'"* → uses
  `semantic_data_search` over the pgvector `embedding` column.

---

## Features

- Natural-language → SQL via an OpenAI Codex **agent loop** over MCP tools
- **Database context files** — local schema notes, rules, definitions, and example SQL
- **Extension-aware** — detects PostGIS / pgvector and adapts the SQL it writes
- **PostGIS spatial** queries (distance / "near" / containment)
- **pgvector semantic search** over embedding columns (`semantic_data_search`)
- Works with **any** connected database through live schema introspection
- Optional FAISS semantic search over schema (drop in `data/schema_hints.json`)
- **Schema Explorer** — browse tables, columns, keys, and relationships
- **Dashboard** — query/execution logs, token usage, and cost analytics
- **Data Analytics** — visualizations and column statistics
- SELECT-only execution with SQL validation and safety checks
- MCP server exposed over **stdio** and optional **HTTP** for IDE clients
- Multi-database support: PostgreSQL, MySQL, SQL Server, Oracle

---

## Project Structure

```
sql-query-assistant/
├── server/                     # Python FastAPI backend
│   ├── main.py                 # Application entry point
│   ├── mcp_stdio_server.py     # MCP server for IDE integration (stdio)
│   ├── requirements.txt        # Python dependencies
│   ├── app/
│   │   ├── config/             # Settings and rate limits
│   │   ├── exceptions/         # Error handling
│   │   ├── middleware/         # Auth + security headers
│   │   ├── models/             # Request/response Pydantic schemas
│   │   ├── routes/             # API endpoints (database, copilot, mcp, monitoring)
│   │   ├── services/           # Core logic (database, Codex agent, context files, logging)
│   │   └── mcp_server/         # MCP server and SQL tools
│   └── data/                   # Runtime stores and uploaded database context
├── demo/                       # Demo DB: PostGIS + pgvector (docker compose + seed)
├── ui/                         # Angular 17 frontend
│   └── src/app/
│       ├── components/
│       │   ├── mcp-agent/          # Copilot Chat interface
│       │   ├── connection-dialog/  # Database connection
│       │   ├── dashboard/          # Logs & cost dashboard
│       │   ├── data-analytics/     # Data visualization & stats
│       │   ├── schema-explorer/    # Database schema browser
│       │   ├── sidebar/            # Navigation
│       │   └── shared/             # Shared components
│       ├── models/             # TypeScript interfaces
│       └── services/           # API, MCP-agent, theme, state services
└── README.md                   # This file
```

## Documentation

- **[server/README.md](server/README.md)** — Backend API, endpoints, configuration
- **[ui/README.md](ui/README.md)** — Frontend setup and components

## MCP Integration

Expose the MCP server to an IDE client over stdio:

```json
{
  "servers": {
    "sql-query-assistant": {
      "command": "python",
      "args": ["server/mcp_stdio_server.py"],
      "type": "stdio"
    }
  }
}
```

Or enable HTTP transport with `MCP_HTTP_ENABLED=true` in `server/.env` and
connect to `http://localhost:2222/mcp`.

---

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy, MCP Python SDK, (optional) FAISS + sentence-transformers
- **Frontend**: Angular 17, Angular Material, Tailwind CSS
- **AI**: OpenAI Codex models through account OAuth (agent LLM; no API key required)
- **Databases**: PostgreSQL (incl. PostGIS + pgvector), MySQL, SQL Server, Oracle
