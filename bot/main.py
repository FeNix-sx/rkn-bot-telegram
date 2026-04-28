"""Local entrypoint for bot startup."""

from __future__ import annotations

import asyncio
import logging

from core import ConfigError, Settings, load_settings

LOGGER = logging.getLogger(__name__)


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


def main() -> None:
    """CLI entrypoint for `python -m bot.main`."""
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        LOGGER.error("Configuration validation failed: %s", exc)
        raise SystemExit(2) from exc

    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
