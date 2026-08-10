"""
Root entrypoint for running FastAPI directly via uvicorn.

Usage:
  uvicorn app:app --reload --port 8005
"""
from services.aml_builder.web.api.main import app

__all__ = ["app"]
