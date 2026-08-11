"""ASGI entrypoint used by Uvicorn and Cloud Run."""

from app.main import create_app

app = create_app()
