"""Database-scoped context-file routes for the Codex SQL agent."""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.services.database_context_service import get_database_context_store

router = APIRouter()


@router.get("/database-context")
async def list_database_context(session_id: str = "", database_name: str = ""):
    """List context files scoped to the active database."""
    return {
        "documents": get_database_context_store().list(
            session_id=session_id,
            database_name=database_name,
        )
    }


@router.post("/database-context")
async def upload_database_context(
    file: UploadFile = File(...),
    session_id: str = Form(""),
    database_name: str = Form(""),
):
    """Upload a UTF-8 context file and make it available on the next AI turn."""
    try:
        payload = await file.read(1_000_001)
        document = get_database_context_store().add(
            filename=file.filename or "context.txt",
            payload=payload,
            session_id=session_id,
            database_name=database_name,
        )
        return {"success": True, "document": document}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/database-context/{doc_id}")
async def delete_database_context(doc_id: str):
    """Delete one uploaded context file."""
    if not get_database_context_store().delete(doc_id):
        raise HTTPException(status_code=404, detail="Context file not found.")
    return {"success": True}
