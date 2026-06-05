"""Metrics_Service WebSocket fan-out (Requirement 7).

A `MetricsManager` holds the set of active operator WebSocket connections
(capped at MAX_CONNECTIONS) and a `broadcast_metrics()` task emits a batched
metric payload to every connection every 500 ms (R7.2). Slow/broken
connections are isolated: a failed send drops that connection without
disrupting the others (R7.5).

The batch (R7.3) now includes:
    active_connections   live operator dashboards connected
    queue_depth          sum of LLEN over all classq:queue:* lists
    allocations_per_sec  confirmed allocations in the most recent 1s window
    timestamp            iso8601 emit time
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import WebSocket

from app.db import redis

logger = logging.getLogger("classq.metrics")

MAX_CONNECTIONS = 100
BROADCAST_INTERVAL_SECONDS = 0.5

# Rolling 1-second window of allocation timestamps (R7.3 allocation rate).
ALLOC_TS_KEY = "classq:metrics:alloc_ts"
ALLOC_WINDOW_MS = 1000
QUEUE_KEY_PATTERN = "classq:queue:*"


async def record_allocation() -> None:
    """Record one confirmed allocation in the rolling 1-second window.

    Called by the registration flow whenever an enrollment is confirmed. The
    metrics broadcaster reads this set to compute allocations_per_sec.
    """
    try:
        client = redis.get_client()
        now_ms = int(time.time() * 1000)
        await client.zadd(ALLOC_TS_KEY, {uuid.uuid4().hex: now_ms})
        # Keep the key from growing unbounded if no one reads it.
        await client.pexpire(ALLOC_TS_KEY, ALLOC_WINDOW_MS * 5)
    except Exception:  # noqa: BLE001 - metrics must never break registration
        logger.debug("record_allocation failed", exc_info=True)


async def _allocations_per_sec(client) -> int:
    """Count allocations within the last ALLOC_WINDOW_MS, evicting older ones."""
    now_ms = int(time.time() * 1000)
    await client.zremrangebyscore(ALLOC_TS_KEY, "-inf", now_ms - ALLOC_WINDOW_MS)
    return int(await client.zcard(ALLOC_TS_KEY))


async def _queue_depth(client) -> int:
    """Sum the length of every registration queue list (R7.3)."""
    total = 0
    async for key in client.scan_iter(match=QUEUE_KEY_PATTERN, count=100):
        try:
            total += int(await client.llen(key))
        except Exception:  # noqa: BLE001 - a non-list key under the pattern
            continue
    return total


class ConnectionLimitReached(Exception):
    """Raised when the connection cap (R7.7) is exceeded."""


class MetricsManager:
    def __init__(self, max_connections: int = MAX_CONNECTIONS) -> None:
        self._max = max_connections
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def connect(self, ws: WebSocket) -> None:
        """Accept and register a WebSocket, enforcing the connection cap (R7.7)."""
        async with self._lock:
            if len(self._connections) >= self._max:
                # Caller is responsible for closing with the right code.
                raise ConnectionLimitReached()
            await ws.accept()
            self._connections.add(ws)
        logger.info("Metrics WS connected (active=%s)", self.active_count)

    async def disconnect(self, ws: WebSocket) -> None:
        """Deregister a WebSocket (R7.4)."""
        async with self._lock:
            self._connections.discard(ws)
        logger.info("Metrics WS disconnected (active=%s)", self.active_count)

    async def _build_batch(self) -> dict:
        queue_depth = 0
        allocations = 0
        try:
            client = redis.get_client()
            queue_depth = await _queue_depth(client)
            allocations = await _allocations_per_sec(client)
        except Exception:  # noqa: BLE001 - degrade gracefully, never crash the loop
            logger.debug("metric aggregation failed", exc_info=True)
        return {
            "active_connections": self.active_count,
            "queue_depth": queue_depth,
            "allocations_per_sec": allocations,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def broadcast_once(self) -> None:
        """Send one batch to all connections; drop any that fail (R7.5)."""
        # Snapshot the connection set so we don't mutate while iterating.
        async with self._lock:
            targets = list(self._connections)
        if not targets:
            return

        batch = await self._build_batch()
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(batch)
            except Exception:  # noqa: BLE001 - isolate one bad connection
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)
            logger.info("Dropped %s dead metrics connection(s)", len(dead))


# Shared singleton used by the app and the broadcast task.
manager = MetricsManager()


async def broadcast_metrics() -> None:
    """Infinite loop: emit a metrics batch to all connections every 500 ms."""
    logger.info("Metrics broadcaster started (interval=%ss)", BROADCAST_INTERVAL_SECONDS)
    while True:
        try:
            await manager.broadcast_once()
        except asyncio.CancelledError:
            logger.info("Metrics broadcaster stopping")
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Metrics broadcast cycle failed")
        await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
