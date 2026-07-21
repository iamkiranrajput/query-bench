"""
MCP Tools Package

Granular tools for natural language to SQL generation pipeline.
Each tool is designed to be invoked by an LLM agent via the MCP protocol.
"""

from .search_tables import search_tables
from .search_columns import search_columns
from .check_relationships import check_relationships
from .preview_data import preview_data
from .generate_sql import generate_sql
from .validate_sql import validate_sql
from .execute_sql import execute_sql
from .explain_sql import explain_sql
from .fix_sql import fix_sql
from .introspect_schema import introspect_schema
from .discover_join_paths import discover_join_paths
from .sample_column_values import sample_column_values
from .connect_database import connect_database
from .get_connection_profile import get_connection_profile
from .analyze_connection_performance import analyze_connection_performance
from .validate_server_compatibility import validate_server_compatibility
from .switch_database import switch_database, list_available_databases
from .detect_extensions import detect_extensions
from .semantic_data_search import semantic_data_search

__all__ = [
    "search_tables",
    "search_columns",
    "check_relationships",
    "preview_data",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "explain_sql",
    "fix_sql",
    "introspect_schema",
    "discover_join_paths",
    "sample_column_values",
    "connect_database",
    "get_connection_profile",
    "analyze_connection_performance",
    "validate_server_compatibility",
    "switch_database",
    "list_available_databases",
    "detect_extensions",
    "semantic_data_search",
]
