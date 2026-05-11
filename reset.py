import aiosqlite
import asyncio

async def reset():
    async with aiosqlite.connect("data/bot.db") as db:
        # 1125747662 — ID galinaymka из твоих логов
        await db.execute("UPDATE users SET xui_email = NULL, xui_uuid = NULL, status = 'new' WHERE tg_id = 1125747662")
        await db.commit()
        print("✅ Galinaymka сброшена в 'new' (теперь в «Нет клиента VPN 🔌» / «Нет аккаунта ТГ 📥»).")

asyncio.run(reset())