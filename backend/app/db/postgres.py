"""Async PostgreSQL connection pool management using asyncpg.

A single shared pool is created at application startup and closed at shutdown.
Use `get_pool()` to obtain the pool from request handlers.
"""

from __future__ import annotations

import asyncpg

from app.core.config import settings

_pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    """Create the shared connection pool (idempotent)."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.postgres_dsn,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
        )
    return _pool


async def disconnect() -> None:
    """Close the shared connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the shared pool, raising if it has not been initialised."""
    if _pool is None:
        raise RuntimeError("PostgreSQL pool is not initialised; call connect() first.")
    return _pool


async def ping() -> bool:
    """Return True if a trivial query against PostgreSQL succeeds."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval("SELECT 1")
    return result == 1
