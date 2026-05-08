"""Test that uses the FIXED get_inbounds() method."""
import asyncio
import logging
from core import XUIAPI, ConfigError, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
LOGGER = logging.getLogger(__name__)

async def main():
    try:
        settings = load_settings()
    except ConfigError as e:
        LOGGER.error("Config: %s", e)
        return

    api = XUIAPI(settings.xui_api_url, settings.xui_username, settings.xui_password)

    try:
        await api.login()
        LOGGER.info("✓ Auth OK")

        # Теперь используем ИСПРАВЛЕННЫЙ метод get_inbounds()
        inbounds = await api.get_inbounds()
        LOGGER.info("✓ Inbounds fetched via get_inbounds(): %d", len(inbounds))

        for ib in inbounds:
            LOGGER.info("  • [%d] %s | clients: %d",
                       ib.get("id"),
                       ib.get("remark") or ib.get("tag") or "unnamed",
                       len(ib.get("clientStats") or []))

        LOGGER.info("✅ API fix verified — get_inbounds() works!")

    except Exception as e:
        LOGGER.error("✗ Test failed: %s", e)
        import traceback
        traceback.print_exc()
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(main())