"""Loguru configuration.

Centralised so every module can simply ``from radar.logconf import log`` and get
a consistently formatted logger that writes to both the console and a rotating
file. Never use ``print`` in this project.
"""

from __future__ import annotations

import sys

from loguru import logger as log

from config import settings

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Idempotently attach console + file sinks. Safe to call repeatedly."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log.remove()  # drop loguru's default handler
    log.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        colorize=True,
    )
    log.add(
        settings.LOG_DIR / "radar.log",
        level="DEBUG",
        rotation="2 MB",
        retention=5,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{line} | {message}",
    )
    _CONFIGURED = True


__all__ = ["log", "configure_logging"]
