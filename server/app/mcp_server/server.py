"""
MCP Server

Model Context Protocol server that exposes SQL tools for LLM agents.
Provides tool registration, execution, and context management.

This implementation can work:
1. With the MCP SDK for protocol-compliant clients
2. Standalone as a tool registry for direct Python integration
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .context import ConversationContextManager, get_context_manager, QueryIntent
from .schema_index import SchemaIndex, get_schema_index, initialize_schema_index
from .tools import (
    search_tables,
    search_columns,
    check_relationships,
    preview_data,
    generate_sql,
    validate_sql,
    execute_sql,
    explain_sql,
    fix_sql,
    introspect_schema,
    discover_join_paths,
    sample_column_values,
    connect_database,
    get_connection_profile,
    analyze_connection_performance,
    validate_server_compatibility,
    detect_extensions,
    semantic_data_search,
)
from .tools.search_tables import TOOL_METADATA as SEARCH_TABLES_META
from .tools.search_columns import TOOL_METADATA as SEARCH_COLUMNS_META
from .tools.check_relationships import TOOL_METADATA as CHECK_RELATIONSHIPS_META
from .tools.preview_data import TOOL_METADATA as PREVIEW_DATA_META
from .tools.generate_sql import TOOL_METADATA as GENERATE_SQL_META
from .tools.validate_sql import TOOL_METADATA as VALIDATE_SQL_META
from .tools.execute_sql import TOOL_METADATA as EXECUTE_SQL_META
from .tools.explain_sql import TOOL_METADATA as EXPLAIN_SQL_META
from .tools.fix_sql import TOOL_METADATA as FIX_SQL_META
from .tools.introspect_schema import TOOL_METADATA as INTROSPECT_SCHEMA_META
from .tools.discover_join_paths import TOOL_METADATA as DISCOVER_JOIN_PATHS_META
from .tools.sample_column_values import TOOL_METADATA as SAMPLE_COLUMN_VALUES_META
from .tools.connect_database import TOOL_METADATA as CONNECT_DATABASE_META
from .tools.get_connection_profile import TOOL_METADATA as CONN_PROFILE_META
from .tools.analyze_connection_performance import TOOL_METADATA as ANALYZE_PERF_META
from .tools.validate_server_compatibility import TOOL_METADATA as VALIDATE_COMPAT_META
from .tools.detect_extensions import TOOL_METADATA as DETECT_EXTENSIONS_META
from .tools.semantic_data_search import TOOL_METADATA as SEMANTIC_DATA_SEARCH_META
from .tools.switch_database import (
    TOOL_METADATA as SWITCH_DB_META,
    LIST_DATABASES_METADATA as LIST_DBS_META,
    switch_database,
    list_available_databases,
    set_database_service as switch_db_set_db,
    set_available_connections,
)
from .tools.domain_resources import DOMAIN_TOOLS

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ToolDefinition:
    """Definition of an MCP tool."""
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable
    
    def to_openai_function(self) -> Dict[str, Any]:
        """Convert to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }
    
    def to_mcp_tool(self) -> Dict[str, Any]:
        """Convert to MCP tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters
        }


@dataclass
class ToolCallResult:
    """Result of a tool call."""
    success: bool
    result: Any
    error: Optional[str] = None


# ============================================================================
# MCP Server
# ============================================================================

class MCPServer:
    """
    MCP Server for SQL tools.
    
    Manages tool registration, execution, and external service integration.
    """
    
    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._db_service = None
        self._llm_client = None
        self._initialized = False
        
        # Register all tools
        self._register_tools()
    
    def _register_tools(self) -> None:
        """Register all SQL tools."""
        # Schema discovery tools
        self._register_tool(
            SEARCH_TABLES_META["name"],
            SEARCH_TABLES_META["description"],
            SEARCH_TABLES_META["parameters"],
            search_tables
        )
        
        self._register_tool(
            SEARCH_COLUMNS_META["name"],
            SEARCH_COLUMNS_META["description"],
            SEARCH_COLUMNS_META["parameters"],
            search_columns
        )
        
        self._register_tool(
            CHECK_RELATIONSHIPS_META["name"],
            CHECK_RELATIONSHIPS_META["description"],
            CHECK_RELATIONSHIPS_META["parameters"],
            check_relationships
        )
        
        # Data access tools
        self._register_tool(
            PREVIEW_DATA_META["name"],
            PREVIEW_DATA_META["description"],
            PREVIEW_DATA_META["parameters"],
            preview_data
        )
        
        # SQL generation tools
        self._register_tool(
            GENERATE_SQL_META["name"],
            GENERATE_SQL_META["description"],
            GENERATE_SQL_META["parameters"],
            generate_sql
        )
        
        self._register_tool(
            VALIDATE_SQL_META["name"],
            VALIDATE_SQL_META["description"],
            VALIDATE_SQL_META["parameters"],
            validate_sql
        )
        
        self._register_tool(
            EXECUTE_SQL_META["name"],
            EXECUTE_SQL_META["description"],
            EXECUTE_SQL_META["parameters"],
            execute_sql
        )
        
        # SQL assistance tools
        self._register_tool(
            EXPLAIN_SQL_META["name"],
            EXPLAIN_SQL_META["description"],
            EXPLAIN_SQL_META["parameters"],
            explain_sql
        )
        
        self._register_tool(
            FIX_SQL_META["name"],
            FIX_SQL_META["description"],
            FIX_SQL_META["parameters"],
            fix_sql
        )
        
        # Iterative discovery tools
        self._register_tool(
            INTROSPECT_SCHEMA_META["name"],
            INTROSPECT_SCHEMA_META["description"],
            INTROSPECT_SCHEMA_META["parameters"],
            introspect_schema
        )
        
        self._register_tool(
            DISCOVER_JOIN_PATHS_META["name"],
            DISCOVER_JOIN_PATHS_META["description"],
            DISCOVER_JOIN_PATHS_META["parameters"],
            discover_join_paths
        )
        
        self._register_tool(
            SAMPLE_COLUMN_VALUES_META["name"],
            SAMPLE_COLUMN_VALUES_META["description"],
            SAMPLE_COLUMN_VALUES_META["parameters"],
            sample_column_values
        )
        
        # Connection tool
        self._register_tool(
            CONNECT_DATABASE_META["name"],
            CONNECT_DATABASE_META["description"],
            CONNECT_DATABASE_META["parameters"],
            connect_database
        )

        # Cross-database tools (only switch + list available connections).
        self._register_tool(
            LIST_DBS_META["name"],
            LIST_DBS_META["description"],
            LIST_DBS_META["parameters"],
            list_available_databases
        )
        self._register_tool(
            SWITCH_DB_META["name"],
            SWITCH_DB_META["description"],
            SWITCH_DB_META["parameters"],
            switch_database
        )

        # Server connection intelligence tools
        self._register_tool(
            CONN_PROFILE_META["name"],
            CONN_PROFILE_META["description"],
            CONN_PROFILE_META["parameters"],
            get_connection_profile
        )
        self._register_tool(
            ANALYZE_PERF_META["name"],
            ANALYZE_PERF_META["description"],
            ANALYZE_PERF_META["parameters"],
            analyze_connection_performance
        )
        self._register_tool(
            VALIDATE_COMPAT_META["name"],
            VALIDATE_COMPAT_META["description"],
            VALIDATE_COMPAT_META["parameters"],
            validate_server_compatibility
        )

        # Database capability + advanced-SQL tools (Postgres extensions:
        # PostGIS spatial, pgvector semantic search).
        self._register_tool(
            DETECT_EXTENSIONS_META["name"],
            DETECT_EXTENSIONS_META["description"],
            DETECT_EXTENSIONS_META["parameters"],
            detect_extensions
        )
        self._register_tool(
            SEMANTIC_DATA_SEARCH_META["name"],
            SEMANTIC_DATA_SEARCH_META["description"],
            SEMANTIC_DATA_SEARCH_META["parameters"],
            semantic_data_search
        )

        # Generic domain-context tools (mirror of the queryBench://*
        # MCP Resources, exposed as tools so the OpenAI Codex agent --
        # which cannot read MCP Resources -- can still reach them).
        for tool_def in DOMAIN_TOOLS:
            self._register_tool(
                tool_def["name"],
                tool_def["description"],
                tool_def["parameters"],
                tool_def["handler"],
            )
        
        # Context management tool
        self._register_tool(
            "get_conversation_context",
            (
                "Get the current conversation context for a chat session. "
                "Returns recent history, current query state, and schema context. "
                "Use this to understand what the user has asked before and "
                "to build refinement queries."
            ),
            {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Chat session ID"
                    }
                },
                "required": ["session_id"]
            },
            self._get_conversation_context
        )
        
        logger.info(f"Registered {len(self._tools)} MCP tools")
    
    def _register_tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable
    ) -> None:
        """Register a single tool."""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler
        )
    
    def initialize(
        self,
        db_service=None,
        llm_client=None,
        schema_hints_path: Optional[str] = None
    ) -> bool:
        """
        Initialize the MCP server with external services.
        
        Args:
            db_service: Database service for query execution
            llm_client: OpenAI-compatible client for LLM calls
            schema_hints_path: Path to schema_hints.json
        
        Returns:
            True if initialization successful
        """
        try:
            # Store service references
            self._db_service = db_service
            self._llm_client = llm_client
            
            # Inject services into tools that need them
            if db_service:
                from .tools.preview_data import set_database_service as pd_set_db
                from .tools.execute_sql import set_database_service as ex_set_db
                from .tools.introspect_schema import set_database_service as is_set_db
                from .tools.discover_join_paths import set_database_service as dj_set_db
                from .tools.discover_join_paths import load_alternative_joins
                from .tools.sample_column_values import set_database_service as sv_set_db
                from .tools.connect_database import set_database_service as cd_set_db
                from .tools.switch_database import set_database_service as sw_set_db
                from .tools.get_connection_profile import set_database_service as cp_set_db
                from .tools.analyze_connection_performance import set_database_service as ap_set_db
                from .tools.validate_server_compatibility import set_database_service as vc_set_db
                pd_set_db(db_service)
                ex_set_db(db_service)
                is_set_db(db_service)
                dj_set_db(db_service)
                sv_set_db(db_service)
                cd_set_db(db_service)
                sw_set_db(db_service)
                cp_set_db(db_service)
                ap_set_db(db_service)
                vc_set_db(db_service)
                from .tools.detect_extensions import set_database_service as de_set_db
                from .tools.semantic_data_search import set_database_service as sds_set_db
                from .tools.search_tables import set_database_service as st_set_db
                from .tools.search_columns import set_database_service as sc_set_db
                de_set_db(db_service)
                sds_set_db(db_service)
                st_set_db(db_service)
                sc_set_db(db_service)

                # Load alternative-joins registry if a curated file is shipped.
                import os
                alt_joins_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                    "data", "alternative_joins.json"
                )
                if os.path.exists(alt_joins_path):
                    load_alternative_joins(alt_joins_path)
            
            if llm_client:
                from .tools.explain_sql import set_llm_client as expl_set_llm
                from .tools.fix_sql import set_llm_client as fix_set_llm
                from .tools.fix_sql import set_llm_model as fix_set_model
                expl_set_llm(llm_client)
                fix_set_llm(llm_client)
                # Pass configured model to fix_sql so it doesn't use hardcoded default
                if hasattr(self, '_model') and self._model:
                    fix_set_model(self._model)
                elif db_service and hasattr(db_service, '_model'):
                    fix_set_model(db_service._model)
            
            # Schema index and context manager are initialized by main.py
            # (FAISS builds in background thread, context manager loads sessions)
            # No need to re-initialize here — just ensure they're accessible
            
            self._initialized = True
            logger.info("MCP server initialized")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize MCP server: {e}")
            return False
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    def list_tools(self) -> List[Dict[str, Any]]:
        """List all available tools in MCP format."""
        return [tool.to_mcp_tool() for tool in self._tools.values()]
    
    def list_tools_openai(self) -> List[Dict[str, Any]]:
        """List all available tools in OpenAI function-calling format."""
        return [tool.to_openai_function() for tool in self._tools.values()]
    
    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> ToolCallResult:
        """
        Execute a tool with the given arguments.
        
        Args:
            name: Tool name
            arguments: Tool arguments as dictionary
        
        Returns:
            ToolCallResult with success status and result/error
        """
        import time as _time
        tool = self._tools.get(name)
        
        if not tool:
            print(f"[MCP] ✖ Unknown tool: {name}")
            return ToolCallResult(
                success=False,
                result=None,
                error=f"Unknown tool: {name}"
            )
        
        # Log tool dispatch with key arguments (hide sensitive data)
        _display_args = {}
        for k, v in arguments.items():
            if k in ("password", "session_id"):
                _display_args[k] = "***"
            elif isinstance(v, str) and len(v) > 100:
                _display_args[k] = v[:100] + "..."
            else:
                _display_args[k] = v
        print(f"[MCP] ▶ {name}({_display_args})")
        _t0 = _time.perf_counter()
        
        try:
            result = tool.handler(**arguments)
            _elapsed = (_time.perf_counter() - _t0) * 1000
            
            # Most tools return dicts with 'success' key
            if isinstance(result, dict):
                _success = result.get("success", True)
                # Build a brief summary of the result
                _summary = ""
                if "row_count" in result:
                    _summary = f"rows={result['row_count']}"
                elif "tables" in result and isinstance(result.get("tables"), list):
                    _summary = f"tables={len(result['tables'])}"
                elif "columns" in result and isinstance(result.get("columns"), list):
                    _summary = f"columns={len(result['columns'])}"
                elif "sql" in result:
                    _summary = f"sql={str(result['sql'])[:60]}"
                elif "valid" in result:
                    _summary = f"valid={result['valid']}"
                elif "session_id" in result:
                    _sid = result['session_id']
                    _summary = f"session={_sid[:16]}..."
                elif "relationships" in result:
                    _rels = result["relationships"]
                    _summary = f"rels={len(_rels) if isinstance(_rels, list) else _rels}"
                elif "explanation" in result:
                    _summary = f"explanation={str(result['explanation'])[:60]}"
                elif "values" in result:
                    _vals = result["values"]
                    _summary = f"values={len(_vals) if isinstance(_vals, list) else _vals}"
                elif "error" in result:
                    _summary = f"error={str(result.get('error', ''))[:80]}"
                
                _icon = "✔" if _success else "✖"
                print(f"[MCP] {_icon} {name} ({_elapsed:.0f}ms) {_summary}")
                
                return ToolCallResult(
                    success=_success,
                    result=result,
                    error=result.get("error")
                )
            
            _elapsed = (_time.perf_counter() - _t0) * 1000
            print(f"[MCP] ✔ {name} ({_elapsed:.0f}ms)")
            return ToolCallResult(
                success=True,
                result=result,
                error=None
            )
            
        except TypeError as e:
            _elapsed = (_time.perf_counter() - _t0) * 1000
            print(f"[MCP] ✖ {name} ({_elapsed:.0f}ms) TypeError: {e}")
            # Argument mismatch
            return ToolCallResult(
                success=False,
                result=None,
                error=f"Invalid arguments for {name}: {e}"
            )
        except Exception as e:
            _elapsed = (_time.perf_counter() - _t0) * 1000
            print(f"[MCP] ✖ {name} ({_elapsed:.0f}ms) {type(e).__name__}: {e}")
            logger.error(f"Tool {name} execution failed: {e}")
            return ToolCallResult(
                success=False,
                result=None,
                error=str(e)
            )
    
    def call_tool_json(self, name: str, arguments_json: str) -> str:
        """
        Execute a tool with JSON string arguments, return JSON result.
        
        Convenience method for MCP protocol integration.
        """
        try:
            arguments = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid JSON arguments: {e}"
            })
        
        result = self.call_tool(name, arguments)
        
        return json.dumps({
            "success": result.success,
            "result": result.result,
            "error": result.error
        }, default=str)
    
    def _get_conversation_context(self, session_id: str) -> Dict[str, Any]:
        """Get conversation context for a session."""
        ctx_manager = get_context_manager()
        return ctx_manager.get_context_for_llm(session_id)
    
    def get_schema_index(self) -> SchemaIndex:
        """Get the schema index instance."""
        return get_schema_index()
    
    def get_context_manager(self) -> ConversationContextManager:
        """Get the context manager instance."""
        return get_context_manager()
    
    def shutdown(self) -> None:
        """Shutdown the MCP server and cleanup resources."""
        try:
            ctx_manager = get_context_manager()
            ctx_manager.stop()
            logger.info("MCP server shutdown complete")
        except Exception as e:
            logger.warning(f"Error during MCP server shutdown: {e}")


# ============================================================================
# Singleton Instance
# ============================================================================

_mcp_server: Optional[MCPServer] = None


def get_mcp_server() -> MCPServer:
    """Get or create the singleton MCP server."""
    global _mcp_server
    
    if _mcp_server is None:
        _mcp_server = MCPServer()
    
    return _mcp_server


# Convenience alias
mcp_server = property(lambda self: get_mcp_server())
