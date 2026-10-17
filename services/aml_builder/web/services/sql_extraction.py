"""Compatibility exports for SQL extraction moved to the pure domain layer."""

from services.aml_builder.web.domain.sql_extract import extract_sql

__all__ = ["extract_sql"]
