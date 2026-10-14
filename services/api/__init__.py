"""HTTP API service (FR-API-01). ``create_app()`` builds the FastAPI application."""

from services.api.app import create_app

__all__ = ["create_app"]
