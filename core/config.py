"""Environment-backed application configuration."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os

REQUIRED_ENV_VARS = (
    "BOT_TOKEN", "XUI_API_URL", "XUI_USERNAME", "XUI_PASSWORD",
    "ADMIN_IDS", "TRIAL_DAYS", "DB_PATH",
)

class ConfigError(ValueError):
    """Raised when configuration is invalid or incomplete."""
    pass

@dataclass(frozen=True)
class Settings:
    """Runtime settings validated from environment variables."""
    bot_token: str
    xui_api_url: str
    xui_username: str
    xui_password: str
    admin_ids: tuple[int, ...]
    trial_days: int
    db_path: str

def _load_dotenv_file(env_path: Path) -> None:
    if not env_path.exists(): return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))

def _require_non_empty(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value: raise ConfigError(f"Missing required environment variable: {name}.")
    return value

def _parse_admin_ids(raw_value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(c.strip()) for c in raw_value.split(",") if c.strip())
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS must contain only integers separated by commas.") from exc
    if not parsed: raise ConfigError("ADMIN_IDS must contain at least one Telegram user id.")
    return parsed

def _parse_trial_days(raw_value: str) -> int:
    try: val = int(raw_value)
    except ValueError as exc: raise ConfigError("TRIAL_DAYS must be a positive integer.") from exc
    if val <= 0: raise ConfigError("TRIAL_DAYS must be greater than zero.")
    return val

def load_settings(env_file: str | Path = ".env") -> Settings:
    _load_dotenv_file(Path(env_file))
    missing = [n for n in REQUIRED_ENV_VARS if not os.getenv(n, "").strip()]
    if missing: raise ConfigError(f"Missing required env var(s): {', '.join(missing)}.")
    return Settings(
        bot_token=_require_non_empty("BOT_TOKEN"),
        xui_api_url=_require_non_empty("XUI_API_URL"),
        xui_username=_require_non_empty("XUI_USERNAME"),
        xui_password=_require_non_empty("XUI_PASSWORD"),
        admin_ids=_parse_admin_ids(_require_non_empty("ADMIN_IDS")),
        trial_days=_parse_trial_days(_require_non_empty("TRIAL_DAYS")),
        db_path=_require_non_empty("DB_PATH"),
    )