"""
Generic MCP/Copilot resource builders.

These builders expose lightweight, generic context that the OpenAI Codex
agent and external MCP clients can consume to understand the connected
database. Any database-specific curated knowledge has been removed -- the
agent now leans on live introspection tools
(``search_tables``, ``introspect_schema``, ``check_relationships``, ...).

Two builders are kept and re-exported so:
  - ``server/mcp_stdio_server.py`` can publish them as ``queryBench://``
    MCP Resources for VSCode Copilot Chat.
  - ``server/app/mcp_server/tools/domain_resources.py`` can wrap them as
    plain MCP tools for clients that cannot read MCP Resources.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_database_context(db_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Generic description of the connected database and tool workflow.

    ``db_info`` is optional connection metadata (host, db name, ...) supplied
    by the caller. The HTTP tool surface passes ``None``; the stdio server
    passes its live ``_db_info`` dict.
    """

    return {
        "server": "OpenAI Codex SQL Assistant",
        "connection": db_info or {"status": "session-managed (auto-injected)"},
        "description": (
            "This MCP server exposes a small toolbox for natural-language "
            "SQL exploration. Use the discovery tools (search_tables, "
            "search_columns, check_relationships, introspect_schema) to "
            "learn the schema, then generate, validate and execute "
            "read-only SELECT queries."
        ),
        "tool_workflow": [
            "1. search_tables -- find tables by natural language",
            "2. search_columns -- inspect columns, types, sample values",
            "3. check_relationships / discover_join_paths -- find JOIN paths",
            "4. generate_sql -- compile a structured plan into SQL",
            "5. validate_sql -- syntax + SELECT-only safety checks",
            "6. execute_sql -- run the query (session_id is auto-injected)",
            "7. fix_sql -- auto-fix from an error message and retry",
        ],
        "session_id_note": (
            "The database is pre-connected. For execute_sql and preview_data, "
            "session_id is automatically injected -- you do NOT need to "
            "provide it."
        ),
    }


def build_schema_domains() -> Dict[str, Any]:
    """Group all known tables by their ``domain`` field from the schema index.

    When no schema-hints file is loaded, the schema index is empty and this
    function returns an informational stub so callers can still call it
    without raising.
    """

    try:
        # Local import keeps this module importable when the MCP server has
        # not finished initialising yet (circular-import safety).
        from app.mcp_server.schema_index import get_schema_index

        idx = get_schema_index()
        if not idx.is_initialized:
            return {
                "total_tables": 0,
                "total_columns": 0,
                "total_relationships": 0,
                "domains": {},
                "tip": (
                    "Schema index not initialised -- no schema_hints.json "
                    "loaded. Use search_tables (live introspection fallback) "
                    "or introspect_schema to discover tables on the "
                    "connected database."
                ),
            }

        domains: Dict[str, Dict[str, Any]] = {}
        for table_name, table_info in idx.tables.items():
            domain = table_info.domain or "general"
            if domain not in domains:
                domains[domain] = {"count": 0, "key_tables": [], "all_tables": []}
            domains[domain]["count"] += 1
            domains[domain]["all_tables"].append(table_name)
            if table_info.relevance_score >= 6 or table_info.row_count > 0:
                domains[domain]["key_tables"].append(
                    {
                        "name": table_name,
                        "business_name": table_info.business_name,
                        "description": (table_info.description or "")[:120],
                        "row_count": table_info.row_count,
                        "relevance": table_info.relevance_score,
                        "columns": len(table_info.columns),
                    }
                )

        for domain_data in domains.values():
            domain_data["key_tables"].sort(key=lambda t: -t["relevance"])

        return {
            "total_tables": len(idx.tables),
            "total_columns": len(idx.columns),
            "total_relationships": len(idx.relationships),
            "domains": domains,
            "tip": (
                "Use search_tables with natural language to find tables. "
                "Use search_columns to inspect a table's columns before "
                "writing SQL."
            ),
        }
    except Exception as exc:  # pragma: no cover -- defensive
        return {"error": str(exc)}
