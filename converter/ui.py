"""Simple web UI served at the default route. It only calls the existing /formats and /convert/... endpoints."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter(include_in_schema=False)
static_files = StaticFiles(directory=STATIC_DIR)


@router.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")
