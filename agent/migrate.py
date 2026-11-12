"""Applies db/migrations/*.sql to Azure SQL in name order, each once.

Run: cd agent && uv run python migrate.py

Uses the admin login (schema changes); the agent's login can only read and write rows.
Applied names are kept in schema_migrations, so reruns are no-ops.
"""

import os
from pathlib import Path

import mssql_python
from dotenv import load_dotenv

from learner_store import DbLogin, connection_string

ROOT = Path(__file__).parent.parent
MIGRATIONS = ROOT / "db" / "migrations"

load_dotenv(ROOT / ".env")


def main() -> None:
    conn_str = connection_string(os.environ, DbLogin.ADMIN)
    if not conn_str:
        raise SystemExit("Azure SQL admin login not set in .env")

    conn = mssql_python.connect(conn_str)
    cur = conn.cursor()
    cur.execute(
        "if object_id('schema_migrations') is null create table schema_migrations"
        " (name nvarchar(200) primary key,"
        " applied_at datetimeoffset not null default sysdatetimeoffset())"
    )
    conn.commit()
    cur.execute("select name from schema_migrations")
    applied = {name for (name,) in cur.fetchall()}

    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue

        # One transaction per file: a failing migration leaves no partial schema.
        try:
            cur.execute(path.read_text())
            cur.execute("insert into schema_migrations (name) values (?)", (path.name,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"applied {path.name}")

    conn.close()


if __name__ == "__main__":
    main()
