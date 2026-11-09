"""Applies supabase/migrations/*.sql in name order, each once.

Run: cd agent && uv run python migrate.py

Applied names are kept in public.schema_migrations, so reruns are no-ops.
"""

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"

load_dotenv(ROOT / ".env")


async def main() -> None:
    # statement_cache_size=0: the Supabase pooler does not support prepared statements.
    conn = await asyncpg.connect(os.environ["SUPABASE_DB_URL"], statement_cache_size=0)
    await conn.execute(
        "create table if not exists schema_migrations"
        " (name text primary key, applied_at timestamptz not null default now());"
        " alter table schema_migrations enable row level security"  # hide from the REST API
    )
    applied = {r["name"] for r in await conn.fetch("select name from schema_migrations")}

    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue

        # One transaction per file: a failing migration leaves no partial schema.
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute("insert into schema_migrations (name) values ($1)", path.name)
        print(f"applied {path.name}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
