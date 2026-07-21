"""
Application configuration loaded from environment variables.

Only the fields required for the OpenAI Codex + MCP tool surface are
declared here. Legacy ``.env`` files that still contain settings from older
features are tolerated via ``extra = "ignore"``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

# Path to the directory that contains the .env file (server/).
SERVER_DIR = Path(__file__).resolve().parent.parent.parent
ENV_FILE = SERVER_DIR / ".env"


class Settings(BaseSettings):
    """Type-safe access to runtime configuration."""

    # -- Application metadata ------------------------------------------------
    app_name: str = Field(default="OpenAI Codex SQL Query Bench", alias="APP_NAME")
    app_version: str = Field(default="2.0.0", alias="APP_VERSION")

    # -- Server / networking -------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=2222, alias="PORT")
    debug: bool = Field(default=False, alias="DEBUG")

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return False

    # CORS allow-list (comma-separated). Used only outside DEBUG mode.
    allowed_origins: str = Field(default="http://localhost:1111", alias="ALLOWED_ORIGINS")

    # -- Logging -------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="logs/app.log", alias="LOG_FILE")

    # When True (default) the file/log handler scrubs RFC-1918 IPv4 octets
    # (10.x.x.x) before they hit ``server/logs/app.log``. Console output is
    # untouched so developers still see real IPs while debugging.
    redact_internal_ips: bool = Field(default=True, alias="REDACT_INTERNAL_IPS")

    # -- Security ------------------------------------------------------------
    # SECRET_KEY MUST be set in .env and be >= 32 chars. validated at startup.
    secret_key: str = Field(default="", alias="SECRET_KEY")
    algorithm: str = Field(default="HS256", alias="ALGORITHM")
    access_token_expire_minutes: int = Field(default=30, alias="ACCESS_TOKEN_EXPIRE_MINUTES")

    # Optional bearer-token gate on /api/*. Empty disables auth (LAN-only deploy).
    api_key: str = Field(default="", alias="API_KEY")

    # Comma-separated list of IPv4/IPv6 addresses of reverse proxies we trust
    # to set CF-Connecting-IP / X-Forwarded-For headers. Requests from any
    # other peer get their headers ignored and are rate-limited by
    # ``request.client.host`` directly.
    trusted_proxies: str = Field(default="127.0.0.1,::1", alias="TRUSTED_PROXIES")

    # -- Sessions / sizing limits -------------------------------------------
    session_expiry_hours: int = Field(default=1, alias="SESSION_EXPIRY_HOURS")
    max_result_rows: int = Field(default=10000, alias="MAX_RESULT_ROWS")
    query_timeout_seconds: int = Field(default=120, alias="QUERY_TIMEOUT_SECONDS")
    enable_performance_profiling: bool = Field(default=True, alias="ENABLE_PERFORMANCE_PROFILING")
    enable_distributed_tracing: bool = Field(default=False, alias="ENABLE_DISTRIBUTED_TRACING")

    # -- Query-log retention --------------------------------------------------
    # Background pruning of ``data/query_logs.db`` retains rows newer than
    # this many days. Set to 0 to disable time-based pruning (the size cap
    # configured in ``query_log_service`` still applies). Default: 30 days.
    query_log_retention_days: int = Field(default=30, alias="QUERY_LOG_RETENTION_DAYS")
    # When True, single-quoted string literals in stored SQL and the raw
    # user query are replaced with ``'***'`` before being written to disk.
    # Default off -- keeps audit fidelity for the hackathon demo.
    query_log_redact_literals: bool = Field(default=False, alias="QUERY_LOG_REDACT_LITERALS")

    # -- OpenAI Codex OAuth token storage ------------------------------------
    # Base64-encoded 32-byte AES-256 key used to encrypt
    # ``.copilot_token.json`` on non-Windows hosts (Windows uses DPAPI).
    # When this is empty and the host is not Windows, the token is NOT
    # written to disk -- the user must re-authenticate after every restart
    # instead of risking a plaintext bearer token. Generate with:
    #   python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
    copilot_token_enc_key: str = Field(default="", alias="COPILOT_TOKEN_ENC_KEY")

    # Default Codex model id (the live account model list can override it).
    copilot_default_model: str = Field(default="gpt-5.6-sol", alias="COPILOT_DEFAULT_MODEL")

    # Agent-loop budget for the Copilot service.
    copilot_agent_wall_clock_seconds: float = Field(
        default=480.0, alias="COPILOT_AGENT_WALL_CLOCK_SECONDS"
    )
    copilot_agent_max_iterations: int = Field(
        default=40, alias="COPILOT_AGENT_MAX_ITERATIONS"
    )
    # When True (default), the agent formats answers as an analyst-style
    # Insight Report / RCA (### Summary / ### Key Insights /
    # ### Root Cause Analysis / ### Recommendations). Set to False for
    # free-form answers matching an interactive Codex chat.
    copilot_structured_response: bool = Field(
        default=True, alias="COPILOT_STRUCTURED_RESPONSE"
    )

    # -- Preset database connection (optional) -------------------------------
    # Convenience for hackathon demos -- when set, the MCP stdio server and
    # the REST `/api/connect` flow can pre-fill a default connection.
    preset_db_name: str = Field(default="", alias="PRESET_DB_NAME")
    preset_db_type: str = Field(default="postgresql", alias="PRESET_DB_TYPE")
    preset_db_host: str = Field(default="", alias="PRESET_DB_HOST")
    preset_db_port: int = Field(default=5432, alias="PRESET_DB_PORT")
    preset_db_database: str = Field(default="", alias="PRESET_DB_DATABASE")
    preset_db_username: str = Field(default="", alias="PRESET_DB_USERNAME")
    preset_db_password: str = Field(default="", alias="PRESET_DB_PASSWORD")

    # -- MCP-over-HTTP (Streamable HTTP transport) ---------------------------
    # Phase 1: expose the MCP server (mcp_stdio_server.py) via HTTP/SSE so
    # external MCP clients (MCP Inspector, Cursor, Claude desktop, ...) can
    # reach it. Disabled by default.
    mcp_http_enabled: bool = Field(default=False, alias="MCP_HTTP_ENABLED")
    mcp_http_path: str = Field(default="/mcp", alias="MCP_HTTP_PATH")
    mcp_http_json_response: bool = Field(default=False, alias="MCP_HTTP_JSON_RESPONSE")

    # Auth mode for /mcp:
    #   "none"   -- open (DEV ONLY, refused at startup unless DEBUG=True)
    #   "static" -- shared bearer token in MCP_HTTP_BEARER_TOKEN
    mcp_http_auth_mode: str = Field(default="static", alias="MCP_HTTP_AUTH_MODE")
    mcp_http_bearer_token: str = Field(default="", alias="MCP_HTTP_BEARER_TOKEN")

    class Config:
        env_file = str(ENV_FILE)
        env_file_encoding = "utf-8"
        case_sensitive = False
        # Tolerate leftover variables from legacy .env files so the
        # hackathon scaffold loads even if the user has not pruned them.
        extra = "ignore"


# Global settings instance
settings = Settings()
