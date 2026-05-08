"""Smoke test: find correct API endpoint for your 3X-UI build."""
import asyncio
import logging
from core import XUIAPI, ConfigError, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
LOGGER = logging.getLogger(__name__)

# Список возможных эндпоинтов для get_inbounds
API_PATHS_TO_TRY = [
    "/panel/api/inbounds",           # Стандартный (FranzKafka/MHS)
    "/panel/api/inbounds/list",      # Старый формат
    "/api/inbounds",                 # Без /panel
    "/inbounds",                     # Короткий
    "/panel/api/inbounds/getAll",    # Альтернатива
]

async def test_endpoint(api: XUIAPI, path: str) -> bool:
    """Try to fetch inbounds from given path. Return True if success."""
    try:
        resp = await api._request("GET", path)
        if isinstance(resp, list):
            LOGGER.info("✓ Found valid endpoint: %s (got %d inbounds)", path, len(resp))
            return True
        elif isinstance(resp, dict) and "obj" in resp and isinstance(resp["obj"], list):
            LOGGER.info("✓ Found valid endpoint: %s (wrapped response, %d inbounds)", path, len(resp["obj"]))
            return True
        else:
            LOGGER.warning("✗ %s returned unexpected format: %s", path, type(resp))
            return False
    except Exception as e:
        LOGGER.debug("✗ %s failed: %s", path, e)
        return False

async def main():
    try:
        settings = load_settings()
    except ConfigError as e:
        LOGGER.error("Config: %s", e)
        return

    api = XUIAPI(settings.xui_api_url, settings.xui_username, settings.xui_password)

    try:
        LOGGER.info("Connecting to %s", settings.xui_api_url)
        await api.login()
        LOGGER.info("✓ Auth OK")

        # Пробуем каждый возможный путь
        found = False
        for path in API_PATHS_TO_TRY:
            LOGGER.info("Trying endpoint: %s", path)
            if await test_endpoint(api, path):
                found = True
                # Сохраняем найденный путь для будущего использования
                LOGGER.info("🎯 Use this path in get_inbounds(): %s", path)
                break

        if not found:
            LOGGER.error("❌ None of the known endpoints worked.")
            LOGGER.info("💡 Manual check: open browser DevTools (F12) → Network tab,")
            LOGGER.info("   login to panel, and see what URL is used for loading inbounds.")
            return

        # Если нашли — покажем превью данных
        # (здесь можно добавить вывод первого inbound для проверки)
        LOGGER.info("✅ Connection test PASSED — ready for next step")

    except Exception as e:
        LOGGER.error("✗ Connection test FAILED: %s", e)
        raise
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(main())