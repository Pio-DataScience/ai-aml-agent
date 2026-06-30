"""
Structured logging configuration for the AML Builder service.

Produces JSON-formatted logs for production (structured, machine-readable)
and human-readable logs for local development.
"""

import logging
import logging.config
import sys
from typing import Any, Dict

from web.services.settings import settings


def get_logging_config() -> Dict[str, Any]:
    """Build the logging configuration dictionary.

    Returns JSON formatter for production and readable formatter for debug.

    Returns:
        Dict[str, Any]: A logging.config.dictConfig-compatible configuration.
    """
    log_level = "DEBUG" if settings.DEBUG else "INFO"

    if settings.DEBUG:
        # Human-readable for local development
        formatter = {
            "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
        formatter_class = "logging.Formatter"
    else:
        # JSON structured for production log aggregators
        formatter = {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        }
        formatter_class = None  # handled by () above

    import os
    os.makedirs("logs", exist_ok=True)

    config: Dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": formatter if formatter_class else formatter,
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "default",
                "level": log_level,
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "logs/app.log",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "default",
                "level": log_level,
                "encoding": "utf-8",
            }
        },
        "root": {
            "handlers": ["console", "file"],
            "level": log_level,
        },
        "loggers": {
            # Quieten noisy third-party libraries
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "oracledb": {"level": "WARNING"},
            "langchain": {"level": "WARNING"},
            "langgraph": {"level": "INFO"},
            "openai": {"level": "WARNING"},
        },
    }

    return config


def setup_logging() -> None:
    """Apply the logging configuration.

    Call once at application startup (in FastAPI lifespan).
    Falls back to basicConfig if python-json-logger is not installed.
    """
    try:
        config = get_logging_config()
        logging.config.dictConfig(config)
    except Exception:
        # Graceful fallback if python-json-logger not installed
        logging.basicConfig(
            level=logging.DEBUG if settings.DEBUG else logging.INFO,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            stream=sys.stdout,
        )

    logging.getLogger(__name__).info(
        "[LOGGING] Structured logging initialized. level=%s debug=%s",
        "DEBUG" if settings.DEBUG else "INFO",
        settings.DEBUG,
    )
