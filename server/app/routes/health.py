"""
Health Check Routes - API status and monitoring
"""

from fastapi import APIRouter
from datetime import datetime
from app.models.schemas import HealthResponse
from app.services.database_service import database_service
from app.services.logger_service import setup_logger
from app.config.settings import settings

logger = setup_logger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint

    - Returns API status
    - Shows active session count
    - Includes timestamp

    The AI engine is OpenAI Codex, which is authenticated per-user at
    runtime via the device-code flow, so there is no server-side LLM
    endpoint to probe here.
    """
    active_sessions = database_service.get_active_session_count()

    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        timestamp=datetime.utcnow(),
        active_sessions=active_sessions,
        llm_reachable=None,
        llm_model=settings.copilot_default_model,
    )
