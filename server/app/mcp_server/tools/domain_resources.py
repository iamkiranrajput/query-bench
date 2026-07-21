"""
Domain-knowledge MCP tools.

After the cleanup only two generic context tools remain. They
wrap the resource builders in ``app.mcp_server.resources`` so MCP clients
that cannot read MCP Resources directly can
still ask for high-level database context.

The previous database-specific tools (``get_concept_mappings``,
``get_critical_rules``, ``get_sample_queries``, ``get_abbreviation_decoder``,
``get_join_intelligence``) have been removed -- the curated JSON files they
read are no longer shipped with the project.

All tools are read-only and take no required arguments.
"""

from __future__ import annotations

from typing import Any, Dict

from app.mcp_server.resources import (
    build_database_context,
    build_schema_domains,
)

# Shared input schema -- no arguments.
_EMPTY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}


def get_database_context() -> Dict[str, Any]:
    """High-level database context and recommended tool workflow."""

    return build_database_context()


def get_schema_domains() -> Dict[str, Any]:
    """All known tables grouped by domain (requires a loaded schema index)."""

    return build_schema_domains()


# ---------------------------------------------------------------------------
# Tool-registration metadata consumed by ``mcp_server/server.py``.
# ---------------------------------------------------------------------------

DOMAIN_TOOLS = [
    {
        "name": "get_database_context",
        "description": (
            "Return high-level context about the connected database: the "
            "recommended tool workflow (search_tables -> generate_sql -> "
            "execute_sql) and the auto-injected session_id contract. Call "
            "this once at the start of a conversation to understand the "
            "environment."
        ),
        "parameters": _EMPTY_SCHEMA,
        "handler": get_database_context,
    },
    {
        "name": "get_schema_domains",
        "description": (
            "Return all tables grouped by their ``domain`` field (extracted "
            "from a curated schema-hints file if one is loaded). Useful for "
            "getting a bird's-eye view of the database. Returns an empty "
            "structure when no schema-hints file is loaded -- use "
            "search_tables / introspect_schema in that case."
        ),
        "parameters": _EMPTY_SCHEMA,
        "handler": get_schema_domains,
    },
]
