"""Outbox_Processor background worker (Requirement 6).

Runs as a long-lived asyncio task started in the FastAPI lifespan. Each cycle:
  1. Claims up to BATCH_SIZE `pending` events ordered by (section_id, sequence)
     so per-section ordering is preserved (R6.6).
  2. "Publishes" each event by logging its payload (a stand-in for a real
     message bus / downstream consumer).
  3. Atomically marks the published rows `processed` with processed_at = now()
     so they are excluded from future cycles (R6.3).

The whole claim->publish->mark sequence runs inside one transaction with
`FOR UPDATE SKIP LOCKED`, so multiple worker instances could run safely without
double-processing the same rows.
"""

from __future__ import annotations

import asyncio
import logging

from app.db import postgres

logger = logging.getLogger("classq.outbox")

BATCH_SIZE = 100
POLL_INTERVAL_SECONDS = 1.0


async def process_once() -> int:
    """Process a single batch of pending outbox events. Returns count processed."""
    pool = postgres.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT event_id, section_id, sequence, event_type, payload
                FROM outbox
                WHERE status = 'pending'
                ORDER BY section_id, sequence
                LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                BATCH_SIZE,
            )

            if not rows:
                return 0

            for row in rows:
                # Simulated publish: log the event payload.
                logger.info(
                    "PUBLISH outbox event_id=%s type=%s section=%s seq=%s payload=%s",
                    row["event_id"],
                    row["event_type"],
                    row["section_id"],
                    row["sequence"],
                    row["payload"],
                )

            event_ids = [row["event_id"] for row in rows]
            await conn.execute(
                """
                UPDATE outbox
                SET status = 'processed', processed_at = now()
                WHERE event_id = ANY($1::uuid[])
                """,
                event_ids,
            )

    return len(rows)


async def process_outbox() -> None:
    """Infinite loop: poll and process the outbox once per second."""
    logger.info("Outbox processor started (batch=%s, interval=%ss)", BATCH_SIZE, POLL_INTERVAL_SECONDS)
    while True:
        try:
            processed = await process_once()
            if processed:
                logger.info("Outbox cycle processed %s event(s)", processed)
        except asyncio.CancelledError:
            logger.info("Outbox processor stopping")
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive on transient errors
            logger.exception("Outbox cycle failed; will retry next interval")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
