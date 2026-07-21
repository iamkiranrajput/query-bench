"""
OpenAI Codex SQL Query Bench - FastAPI Entry Point

Minimal backend that exposes:
  * OpenAI Codex agent loop (`/api/copilot/*`) backed by MCP tools.
  * MCP tool surface for direct invocation from the UI (`/api/mcp/*`).
  * Database connection management (`/api/connect`, `/api/disconnect`, ...).
  * Health & monitoring endpoints.
  * Debug / query-log endpoints used by the dashboard analytics panel.

All legacy data, alternative LLM fallback paths, external bot integrations
and learning-service plumbing have been removed for the hackathon release.
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# HuggingFace / sentence-transformers offline hints. The Copilot service
# never downloads models at runtime; these env vars keep the FAISS-backed
# schema index code paths from hitting the network in restricted
# environments. They are safe to ignore when the embedding cache is empty.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

import concurrent.futures
import time as _time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.rate_limits import limiter
from app.config.settings import settings
from app.exceptions.handlers import setup_exception_handlers
from app.middleware import setup_middleware
from app.middleware.auth import ApiKeyAuthMiddleware
from app.routes import (
    copilot_routes,
    database,
    debug_logs,
    health,
    knowledge,
    mcp_direct,
)
from app.routes.monitoring import router as monitoring_router
from app.services.cache_service import cache_manager
from app.services.logger_service import setup_logger
from app.services.performance_service import performance_monitor

logger = setup_logger(__name__)


_PLACEHOLDER_SECRETS = {
    "change-this-secret-key",
    "change-this-to-a-random-secure-string-in-production",
}


def validate_configuration() -> None:
    """Fail fast if mandatory configuration is missing or insecure."""

    errors: list[str] = []

    secret_key = settings.secret_key or ""
    if not secret_key or secret_key in _PLACEHOLDER_SECRETS or len(secret_key) < 32:
        errors.append(
            "SECRET_KEY must be set in .env with a secure random value (>= 32 chars). "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    if errors:
        logger.error("=" * 60)
        logger.error("CONFIGURATION ERRORS DETECTED:")
        for err in errors:
            logger.error("  [FAIL] %s", err)
        logger.error("=" * 60)
        sys.exit(1)

    logger.info("[OK] Configuration validation passed")

    if not (settings.api_key or "").strip():
        logger.warning(
            "[WARN] API_KEY is not set in .env -- /api/* endpoints are open. "
            "Set API_KEY in .env to require a Bearer token on every request."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: validate config, warm the MCP schema index, dispose on shutdown."""

    logger.info("=" * 60)
    logger.info("Starting OpenAI Codex SQL Query Bench")

    validate_configuration()

    cache_manager.clear_all()
    logger.debug("All caches cleared on startup")

    startup_start = _time.perf_counter()

    try:
        from app.mcp_server import get_mcp_server
        from app.mcp_server.schema_index import initialize_schema_index
        from app.services.database_service import database_service

        # ------------------------------------------------------------------
        # The schema index is OPTIONAL. When data/schema_hints.json exists
        # the MCP server uses FAISS-backed semantic search; otherwise the
        # tools fall back to live `introspect_schema` calls against the
        # connected database. We try to load it in a background thread so
        # the API is responsive even if FAISS init is slow.
        # ------------------------------------------------------------------
        from pathlib import Path

        schema_hints_path = Path(__file__).parent / "data" / "schema_hints.json"

        def _init_schema_index() -> None:
            t0 = _time.perf_counter()
            if not schema_hints_path.exists():
                logger.info(
                    "[SKIP] %s not present -- MCP tools will use live "
                    "database introspection.",
                    schema_hints_path.name,
                )
                return
            try:
                initialize_schema_index(str(schema_hints_path))
                elapsed = (_time.perf_counter() - t0) * 1000
                logger.info("[OK] MCP schema index initialized (%.0fms)", elapsed)
            except Exception as exc:  # noqa: BLE001 -- defensive
                logger.warning("[WARN] Schema index init failed: %s", exc)

        def _init_mcp_server() -> None:
            t0 = _time.perf_counter()
            mcp_server = get_mcp_server()
            mcp_server.initialize(
                db_service=database_service,
                schema_hints_path=(
                    str(schema_hints_path) if schema_hints_path.exists() else None
                ),
            )
            elapsed = (_time.perf_counter() - t0) * 1000
            logger.info("[OK] MCP server initialized (%.0fms)", elapsed)

        def _init_context_manager() -> None:
            t0 = _time.perf_counter()
            from app.mcp_server.context import get_context_manager

            get_context_manager()
            elapsed = (_time.perf_counter() - t0) * 1000
            logger.info("[OK] Context manager initialized (%.0fms)", elapsed)

        import threading

        faiss_thread = threading.Thread(
            target=_init_schema_index, name="faiss-init", daemon=True
        )
        faiss_thread.start()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="startup"
        ) as pool:
            for fut in concurrent.futures.as_completed(
                {
                    pool.submit(_init_context_manager): "context-manager",
                }
            ):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[WARN] Startup task failed: %s", exc)

        _init_mcp_server()

    except Exception as exc:  # noqa: BLE001 -- defensive bootstrap
        logger.warning("MCP initialization failed: %s", exc)
        import traceback

        logger.debug(traceback.format_exc())

    elapsed_ms = (_time.perf_counter() - startup_start) * 1000
    logger.info(
        "[OK] Server ready in %.0fms - Features: OpenAI Codex | MCP Tools | DB Manager",
        elapsed_ms,
    )
    logger.info("API Docs: /api/docs | Monitoring: /api/monitoring/health")
    logger.info("=" * 60)

    from app.mcp_server.http_app import mcp_http_lifespan

    async with mcp_http_lifespan():
        yield

    logger.info("Shutting down OpenAI Codex SQL Query Bench")
    try:
        await _shutdown_resources()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Shutdown encountered error, forcing cleanup: %s", exc)
        try:
            await _shutdown_resources(force=True)
        except Exception as force_exc:  # noqa: BLE001
            logger.error("Force cleanup also failed: %s", force_exc)


async def _shutdown_resources(force: bool = False) -> None:
    """Dispose database engines, cache cleaners and context manager workers."""

    try:
        cache_manager.shutdown()
        logger.debug("Cache cleanup thread stopped")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error stopping cache cleanup thread: %s", exc)

    try:
        from app.mcp_server.context import get_context_manager

        get_context_manager().stop()
        logger.debug("Context manager cleanup thread stopped")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error stopping context manager: %s", exc)

    try:
        from app.services.database_service import database_service

        sessions = list(database_service.sessions.items())
        if sessions:
            logger.debug("Disposing %d active database sessions", len(sessions))

            async def _dispose(session_id: str, engine) -> None:
                try:
                    if force:
                        engine.dispose()
                    else:
                        await asyncio.wait_for(
                            asyncio.to_thread(engine.dispose), timeout=1.0
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "DB engine dispose timed out for session %s", session_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Error disposing engine for session %s: %s", session_id, exc
                    )

            await asyncio.gather(
                *(_dispose(sid, session.engine) for sid, session in sessions),
                return_exceptions=True,
            )
            database_service.sessions.clear()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error disposing database sessions: %s", exc)

    try:
        metrics = performance_monitor.get_metrics_summary()
        logger.info("Final metrics: %s", metrics.get("summary", {}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Error gathering final metrics: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenAI Codex SQL Query Bench",
    description=(
        "Natural-language SQL assistant powered by OpenAI Codex "
        "models talking to a Model Context Protocol (MCP) tool surface."
    ),
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

# Rate limiter must be attached before middleware that depends on app.state
app.state.limiter = limiter

setup_exception_handlers(app)
setup_middleware(app)

# CORS -- localhost origins in DEBUG mode, otherwise explicit allow-list only.
_env_origins = [o.strip() for o in (settings.allowed_origins or "").split(",") if o.strip()]
if settings.debug:
    _dev_origins = [
        "http://localhost:1111",
        "http://127.0.0.1:1111",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        # MCP Inspector default ports (UI + proxy)
        "http://localhost:6274",
        "http://127.0.0.1:6274",
        "http://localhost:6277",
        "http://127.0.0.1:6277",
    ]
    _all_origins = list(dict.fromkeys(_dev_origins + _env_origins))
    _allow_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
else:
    _all_origins = _env_origins if _env_origins else ["http://localhost:1111"]
    _allow_origin_regex = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=_all_origins,
    allow_origin_regex=_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        # MCP Streamable HTTP transport headers (required by MCP Inspector / browser clients)
        "Accept",
        "mcp-session-id",
        "mcp-protocol-version",
        "Last-Event-ID",
    ],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)

if settings.api_key:
    app.add_middleware(ApiKeyAuthMiddleware, api_key=settings.api_key)
    logger.info("[OK] API Key authentication ENABLED -- all /api/* endpoints require Bearer token")

# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

app.include_router(database.router, prefix="/api", tags=["Database"])
app.include_router(health.router, prefix="/api", tags=["Health"])
app.include_router(monitoring_router, prefix="/api", tags=["Monitoring"])
app.include_router(mcp_direct.router, prefix="/api", tags=["MCP Tools"])
app.include_router(copilot_routes.router, prefix="/api", tags=["OpenAI Codex"])
app.include_router(knowledge.router, prefix="/api", tags=["Database Context"])
app.include_router(debug_logs.router, tags=["Debug Logs"])

# MCP-over-HTTP transport (Streamable HTTP) -- mounted last so REST routes win.
# Gated by MCP_HTTP_ENABLED in .env; see app/mcp_server/http_app.py.
from app.mcp_server.http_app import mount_mcp_http

mount_mcp_http(app)


@app.get("/")
async def root():
    """Root endpoint -- API information with system status."""

    metrics = performance_monitor.get_metrics_summary()
    cache_stats = cache_manager.get_stats()

    return {
        "name": "OpenAI Codex SQL Query Bench",
        "version": "2.0.0",
        "status": "running",
        "docs": "/api/docs",
        "monitoring": "/api/monitoring/health",
        "uptime": metrics.get("summary", {}).get("uptime_formatted", "0s"),
        "total_operations": metrics.get("summary", {}).get("total_operations", 0),
        "cache_hit_rate": f"{cache_stats.get('query', {}).get('hit_rate', 0)}%",
    }


if __name__ == "__main__":
    # Fix Python 3.13+ Windows asyncio crash (AssertionError in proactor_events.py)
    if sys.platform == "win32" and sys.version_info >= (3, 13):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    _enable_reload = os.getenv("RELOAD", "false").lower() == "true"
    _host = settings.host or "127.0.0.1"
    _port = settings.port or 2222

    # Trust X-Forwarded-* headers when running behind a reverse proxy
    # (nginx, Azure Front Door, ...) so request.url.scheme is "https" and
    # client IPs reflect the original caller. Restrict trusted upstream
    # IPs via FORWARDED_ALLOW_IPS (default "*" -- override in production).
    _proxy_headers = os.getenv("UVICORN_PROXY_HEADERS", "true").lower() == "true"
    _forwarded_allow = os.getenv("FORWARDED_ALLOW_IPS", "*")

    if _enable_reload:
        uvicorn.run(
            "main:app",
            host=_host,
            port=_port,
            reload=True,
            reload_dirs=["app"],
            reload_includes=["*.py"],
            reload_excludes=["__pycache__", "*.pyc"],
            reload_delay=0.25,
            log_level="info",
            timeout_graceful_shutdown=5,
            proxy_headers=_proxy_headers,
            forwarded_allow_ips=_forwarded_allow,
        )
    else:
        uvicorn.run(
            app,
            host=_host,
            port=_port,
            log_level="info",
            timeout_graceful_shutdown=5,
            proxy_headers=_proxy_headers,
            forwarded_allow_ips=_forwarded_allow,
        )
