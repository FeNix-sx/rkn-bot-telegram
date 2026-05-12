"""Environment-backed application configuration."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import logging
import os

LOGGER = logging.getLogger(__name__)
# Корень репозитория (рядом с core/, bot/), чтобы .env находился независимо от cwd
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

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
    xui_inbound_tag: str
    xui_inbound_id: int | None
    xui_vless_host: str | None
    admin_ids: tuple[int, ...]
    trial_days: int
    db_path: str
    xui_keepalive_seconds: int

def _load_dotenv_file(env_path: Path) -> None:
    """Подставляет переменные из файла в os.environ (перезапись), иначе setdefault
    не обновит ключ, уже объявленный в среде запуска пустой строкой."""
    if not env_path.exists(): return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", maxsplit=1)
        os.environ[key.strip()] = value.strip().strip("'\"")

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

def _xui_inbound_tag() -> str:
    v = os.getenv("XUI_INBOUND_TAG", "").strip()
    return v if v else "vless_reality"

def _xui_inbound_id() -> int | None:
    v = os.getenv("XUI_INBOUND_ID", "").strip()
    if not v:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except ValueError:
        return None

def _xui_vless_host() -> str | None:
    v = os.getenv("XUI_VLESS_HOST", "").strip()
    return v if v else None


def _xui_keepalive_seconds() -> int:
    """Интервал тихого опроса панели (сек). 0 — выключено. По умолчанию 3600."""
    v = os.getenv("XUI_KEEPALIVE_SECONDS", "").strip()
    if not v:
        return 3600
    try:
        n = int(v)
    except ValueError:
        return 3600
    return max(0, n)

def _resolve_env_path(env_file: str | Path) -> Path:
    p = Path(env_file)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def load_settings(env_file: str | Path = ".env") -> Settings:
    env_path = _resolve_env_path(env_file)
    if not env_path.exists():
        LOGGER.warning(
            "Файл %s не найден (cwd=%s). Переменные только из окружения процесса.",
            env_path,
            Path.cwd(),
        )
    _load_dotenv_file(env_path)
    missing = [n for n in REQUIRED_ENV_VARS if not os.getenv(n, "").strip()]
    if missing: raise ConfigError(f"Missing required env var(s): {', '.join(missing)}.")
    return Settings(
        bot_token=_require_non_empty("BOT_TOKEN"),
        xui_api_url=_require_non_empty("XUI_API_URL"),
        xui_username=_require_non_empty("XUI_USERNAME"),
        xui_password=_require_non_empty("XUI_PASSWORD"),
        xui_inbound_tag=_xui_inbound_tag(),
        xui_inbound_id=_xui_inbound_id(),
        xui_vless_host=_xui_vless_host(),
        admin_ids=_parse_admin_ids(_require_non_empty("ADMIN_IDS")),
        trial_days=_parse_trial_days(_require_non_empty("TRIAL_DAYS")),
        db_path=_require_non_empty("DB_PATH"),
        xui_keepalive_seconds=_xui_keepalive_seconds(),
    )