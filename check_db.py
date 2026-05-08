import asyncio
import aiosqlite

async def main():
    async with aiosqlite.connect('data/bot.db') as db:
        async with db.execute('PRAGMA table_info(users)') as cur:
            print("Колонки таблицы users:")
            async for row in cur:
                print(f"  • {row[1]} ({row[2]})")

asyncio.run(main())