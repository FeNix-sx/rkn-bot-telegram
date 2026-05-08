import os
import asyncio
import aiosqlite

async def main():
    db_path = os.path.abspath("data/bot.db")
    print(f"📁 Путь: {db_path}")
    print(f"📄 Файл есть: {os.path.exists(db_path)}")

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                tables = [r[0] async for r in cur]
                print(f"📊 Таблицы: {tables}")

                if "users" in tables:
                    print("🔍 Колонки 'users':")
                    async with db.execute("PRAGMA table_info(users)") as col_cur:
                        async for row in col_cur:
                            print(f"  • {row[1]} ({row[2]})")
                else:
                    print("❌ Таблица 'users' отсутствует!")
    except Exception as e:
        print(f"💥 Ошибка: {e}")

asyncio.run(main())