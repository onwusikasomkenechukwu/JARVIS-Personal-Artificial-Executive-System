"""PostgreSQL connection + schema bootstrap (asyncpg).

asyncpg is imported lazily so the package and its pure-logic tests import without a
running database.
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def connect():
    import asyncpg

    return await asyncpg.connect(settings.database_url)


async def create_pool():
    import asyncpg

    return await asyncpg.create_pool(settings.database_url)


async def init_schema(conn) -> None:
    """Create the facts table if it does not exist. Pass an asyncpg connection."""
    await conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
