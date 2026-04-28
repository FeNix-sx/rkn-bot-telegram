"""Local entrypoint for bot startup."""

from __future__ import annotations

import asyncio
import logging
import sys

from core import ConfigError, Settings, load_settings

LOGGER = logging.getLogger(__name__)
MIN_PYTHON = (3, 12)


async def run(settings: Settings) -> None:
    """Run minimal startup flow for day-1 skeleton."""
    LOGGER.info(
        "Bot skeleton startup complete",
        extra={
            "trial_days": settings.trial_days,
            "db_path": settings.db_path,
        },
    )


def configure_logging() -> None:
    """Configure basic stdout logging for local run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def validate_python_version() -> None:
    """Fail fast if interpreter version is below project minimum."""
    if sys.version_info < MIN_PYTHON:
        version = ".".join(str(part) for part in MIN_PYTHON)
        raise SystemExit(f"Python {version}+ is required to run this bot.")


def main() -> None:
    """CLI entrypoint for `python -m bot.main`."""
    configure_logging()
    validate_python_version()
    try:
        settings = load_settings()
    except ConfigError as exc:
        LOGGER.error("Configuration validation failed: %s", exc)
        raise SystemExit(2) from exc

    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
