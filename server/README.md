# SQL Query Assistant — Backend Server

FastAPI backend that pairs **OpenAI Codex** models with a **Model
Context Protocol (MCP)** tool surface to turn natural-language questions into
safe, validated SQL against your own relational database.

## Quick Start

```bash
# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1  # Windows
source venv/bin/activate     # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — set SECRET_KEY (required, >= 32 chars).
# OpenAI Codex is authenticated at runtime via device-code flow (no API key in .env).

# Run server
python main.py
```

Server runs at: `http://localhost:2222`
API docs: `http://localhost:2222/api/docs`

> The default port comes from `PORT=` in `.env`. Override at any time, e.g.
> `PORT=2222 python main.py`.

### Stopping the Server
```bash
# Press Ctrl+C in the terminal, then:
deactivate
```

## Project Structure

```
server/
├── main.py                        # FastAPI application entry point
├── mcp_stdio_server.py            # MCP stdio server for IDE integration
├── app/
│   ├── config/
│   │   ├── settings.py            # Pydantic settings from .env
│   │   └── rate_limits.py         # API rate limiting configuration
│   ├── exceptions/                # Custom exception classes & handlers
│   ├── middleware/
│   │   ├── auth.py                # Optional API-key bearer auth
│   │   └── security.py            # Security headers middleware
│   ├── models/
│   │   ├── schemas.py             # Request/response Pydantic models
│   │   ├── mcp_direct.py          # MCP direct-call request/response models
│   │   └── trace.py               # Query trace types
│   ├── routes/
│   │   ├── database.py            # /api/connect, /api/disconnect, /api/preset-connect, ...
│   │   ├── copilot_routes.py      # /api/copilot/* (chat, auth, models, logs)
│   │   ├── mcp_direct.py          # /api/mcp/* (direct tool invocation)
│   │   ├── health.py              # /api/health
│   │   ├── monitoring.py          # /api/monitoring/* (metrics, logs, cache)
│   │   └── debug_logs.py          # /api/debug/logs (dashboard analytics)
│   ├── services/
│   │   ├── database_service.py    # Connection pool management
│   │   ├── query_log_service.py   # SQLite query/Copilot logging
│   │   ├── cache_service.py       # Query plan caching
│   │   ├── performance_service.py # Performance monitoring
│   │   ├── logger_service.py      # Logging configuration
│   │   └── copilot/
│   │       └── service.py         # OpenAI Codex agent loop (MCP tool calling)
│   └── mcp_server/
│       ├── server.py              # MCP server setup & tool registration
│       ├── schema_index.py        # Optional FAISS semantic search index
│       ├── context.py             # MCP session/context management
│       ├── resources.py           # MCP resource builders
│       ├── system_prompt.py       # Generic SQL-assistant system prompt
│       ├── http_app.py            # Optional MCP-over-HTTP (Streamable HTTP)
│       └── tools/                 # MCP tool implementations
│           ├── search_tables.py, search_columns.py
│           ├── check_relationships.py, discover_join_paths.py
│           ├── generate_sql.py, validate_sql.py, execute_sql.py
│           ├── explain_sql.py, fix_sql.py
│           ├── preview_data.py, sample_column_values.py
│           ├── introspect_schema.py, check_db_integrity.py
│           ├── connect_database.py, switch_database.py
│           ├── get_connection_profile.py, analyze_connection_performance.py
│           └── validate_server_compatibility.py
└── data/                          # Runtime SQLite stores (auto-created)
    ├── query_logs.db              # Query & Copilot execution logs
    └── sessions.db                # MCP session state
```

> The MCP tools work against **any** connected database via live
> introspection. If you drop a `data/schema_hints.json` file in place, the
> server will additionally build a FAISS semantic index over it at startup;
> otherwise it falls back to live `introspect_schema` calls.

## Configuration

### Environment Variables (.env)

See `.env.example` for all available settings. Key groups:

| Group | Variables | Required |
|-------|-----------|----------|
| Server | `HOST`, `PORT`, `DEBUG` | Defaults provided |
| Security | `SECRET_KEY` | **Yes** (min 32 chars) |
| Auth (optional) | `API_KEY` | Optional bearer gate on `/api/*` |
| OpenAI Codex | `COPILOT_DEFAULT_MODEL`, `COPILOT_TOKEN_ENC_KEY`, `COPILOT_AGENT_*` | Defaults provided; OpenAI OAuth device login at runtime |
| Sessions / limits | `SESSION_EXPIRY_HOURS`, `MAX_RESULT_ROWS`, `QUERY_TIMEOUT_SECONDS` | Defaults provided |
| Query-log retention | `QUERY_LOG_RETENTION_DAYS`, `QUERY_LOG_REDACT_LITERALS` | Defaults provided |
| Preset DB (optional) | `PRESET_DB_*` | Optional |
| MCP-over-HTTP (optional) | `MCP_HTTP_ENABLED`, `MCP_HTTP_AUTH_MODE`, `MCP_HTTP_BEARER_TOKEN` | Disabled by default |

## API Endpoints

### Database Connection
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/connect` | Connect to a database (PostgreSQL/MySQL/MSSQL/Oracle) |
| POST | `/api/disconnect` | Close a database connection |
| POST | `/api/list-databases` | List databases on a server |
| POST | `/api/get-tables` | Get table names for a session |
| GET | `/api/preset-connection` | Get preset connection metadata (no password) |
| POST | `/api/preset-connect` | Auto-connect using server-side preset credentials |
| POST | `/api/generate-schema` | Build/refresh the schema index for a session |
| GET | `/api/health` | Health check |

### OpenAI Codex Chat
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/copilot/auth/device-code` | Start OpenAI device-code login |
| POST | `/api/copilot/auth/poll` | Poll for device-code completion |
| POST | `/api/copilot/auth/disconnect` | Clear stored Codex OAuth token |
| GET | `/api/copilot/models` | List Codex models available to the signed-in account |
| POST | `/api/copilot/chat` | Run the agent loop (NL → MCP tools → answer) |
| POST | `/api/copilot/chat/stream` | Streaming variant of `/chat` |
| GET | `/api/copilot/logs` | Copilot execution logs (dashboard) |
| POST | `/api/copilot/recalculate-costs` | Recompute token/cost totals |
| DELETE | `/api/copilot/session/{session_id}` | Clear a Copilot chat session |

### MCP Tools (direct invocation)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/mcp/tools` | List available MCP tools & schemas |
| POST | `/api/mcp/call` | Invoke a single MCP tool |
| POST | `/api/mcp/chain` | Invoke a sequence of MCP tools |

### Monitoring & Debug
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/monitoring/health` | Performance health |
| GET | `/api/monitoring/metrics` | Performance metrics |
| GET | `/api/monitoring/cache-stats` | Cache statistics |
| GET | `/api/monitoring/query-logs` | Stored query logs |
| GET | `/api/monitoring/execution-logs/chat` | Chat execution logs (dashboard) |
| GET | `/api/debug/logs` | Debug logs for the analytics panel |

## MCP Integration (IDE / stdio)

`mcp_stdio_server.py` exposes the same tool surface over stdio so MCP clients
(VS Code, Cursor, Claude Desktop, MCP Inspector) can drive it directly:

```jsonc
{
  "mcpServers": {
    "sql-query-assistant": {
      "command": "python",
      "args": ["mcp_stdio_server.py"],
      "cwd": "server"
    }
  }
}
```

To expose MCP over HTTP instead, set `MCP_HTTP_ENABLED=true` in `.env` and
point your client at `http://localhost:2222/mcp`.
