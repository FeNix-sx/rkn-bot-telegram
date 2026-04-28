"""Environment-backed application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


REQUIRED_ENV_VARS = (
    "BOT_TOKEN",
    "XUI_API_URL",
    "XUI_API_TOKEN",
    "ADMIN_IDS",
    "TRIAL_DAYS",
    "DB_PATH",
)


class ConfigError(ValueError):
    """Raised when configuration is invalid or incomplete."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings validated from environment variables."""

    bot_token: str
    xui_api_url: str
    xui_api_token: str
    admin_ids: tuple[int, ...]
    trial_days: int
    db_path: str


def _load_dotenv_file(env_path: Path) -> None:
    """Load key/value pairs from .env into process env if not already set."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _require_non_empty(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            "Check your .env file."
        )
    return value


def _parse_admin_ids(raw_value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(
            int(chunk.strip())
            for chunk in raw_value.split(",")
            if chunk.strip()
        )
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS must contain only integers separated by commas.") from exc

    if not parsed:
        raise ConfigError("ADMIN_IDS must contain at least one Telegram user id.")
    return parsed


def _parse_trial_days(raw_value: str) -> int:
    try:
        trial_days = int(raw_value)
    except ValueError as exc:
        raise ConfigError("TRIAL_DAYS must be a positive integer.") from exc

    if trial_days <= 0:
        raise ConfigError("TRIAL_DAYS must be greater than zero.")
    return trial_days


def load_settings(env_file: str | Path = ".env") -> Settings:
    """Load and validate runtime settings from .env and process env."""
    _load_dotenv_file(Path(env_file))

    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name, "").strip()]
    if missing:
        names = ", ".join(missing)
        raise ConfigError(f"Missing required environment variable(s): {names}.")

    return Settings(
        bot_token=_require_non_empty("BOT_TOKEN"),
        xui_api_url=_require_non_empty("XUI_API_URL"),
        xui_api_token=_require_non_empty("XUI_API_TOKEN"),
        admin_ids=_parse_admin_ids(_require_non_empty("ADMIN_IDS")),
        trial_days=_parse_trial_days(_require_non_empty("TRIAL_DAYS")),
        db_path=_require_non_empty("DB_PATH"),
    )
