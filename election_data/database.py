"""PostgreSQL access and schema management (psycopg 3)."""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from election_data import config

log = logging.getLogger(__name__)


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url or config.database_url(), row_factory=dict_row, autocommit=autocommit)


@contextmanager
def transaction(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection, commit on success, roll back on error."""
    conn = connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_schema(conn: psycopg.Connection) -> list[str]:
    """Apply sql/schema.sql (idempotent) then any unapplied sql/migrations/*.sql.

    Migrations are named NNN_description.sql and are recorded in
    schema_migrations so each runs exactly once.
    """
    applied: list[str] = []
    base = config.SCHEMA_SQL.read_text(encoding="utf-8")
    conn.execute(base)
    conn.execute(
        "INSERT INTO schema_migrations (version, checksum) VALUES ('000_base_schema', %s) "
        "ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = NOW()",
        (_checksum(base),),
    )
    applied.append("000_base_schema")

    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    for path in sorted(config.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("Applying migration %s", version)
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
            (version, _checksum(sql)),
        )
        applied.append(version)
    conn.commit()
    return applied
