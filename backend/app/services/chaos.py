"""Chaos_Controller (Requirement 8).

Generates a burst of simulated registration requests against a target section to
stress-test the seat-allocation pipeline. Each simulated request is driven
through the real `registration.register()` flow using a distinct chaos-bot
student id, so the no-overselling invariant is exercised end-to-end.

Single active simulation at a time (R8.7); stop signals the running burst to
cease generating new requests (R8.4).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from app.db import postgres
from app.services import registration

logger = logging.getLogger("classq.chaos")

# Bounds (R8.1 / R8.6): keep the local burst sane.
MIN_VOLUME = 1
MAX_VOLUME = 100_000
# Cap concurrency so we hammer hard but don't exhaust the connection pool.
MAX_CONCURRENCY = 50


@dataclass
class ChaosState:
    active: bool = False
    section_id: str | None = None
    volume: int = 0
    started: int = 0
    enrolled: int = 0
    waitlisted: int = 0
    section_full: int = 0
    rejected: int = 0
    errors: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)

    def summary(self) -> dict:
        return {
            "active": self.active,
            "section_id": self.section_id,
            "volume": self.volume,
            "started": self.started,
            "enrolled": self.enrolled,
            "waitlisted": self.waitlisted,
            "section_full": self.section_full,
            "rejected": self.rejected,
            "errors": self.errors,
        }


_state = ChaosState()
_stop_event = asyncio.Event()


def status() -> dict:
    return _state.summary()


class ChaosValidationError(Exception):
    """Invalid chaos activation configuration (R8.6)."""


class ChaosAlreadyActive(Exception):
    """A simulation is already running (R8.7)."""


async def _ensure_chaos_students(section_id: str, volume: int) -> None:
    """Insert chaos-bot student rows so the enrollment FK is satisfied.

    Registration inserts into `enrollments(student_id ...)` which references
    `students`. We pre-create deterministic chaos-bot students (idempotent).
    """
    pool = postgres.get_pool()
    rows = [
        (f"chaos-bot-{i}", f"chaos-bot-{i}", f"Chaos Bot {i}")
        for i in range(volume)
    ]
    async with pool.acquire() as conn:
        # student_id is a UUID PK; chaos bots use a deterministic UUID derived
        # from their name so repeated runs reuse the same rows.
        await conn.executemany(
            """
            INSERT INTO students (student_id, external_id, display_name)
            VALUES ($1::uuid, $2, $3)
            ON CONFLICT (student_id) DO NOTHING
            """,
            [
                (str(uuid.uuid5(uuid.NAMESPACE_DNS, name)), ext, disp)
                for (name, ext, disp) in rows
            ],
        )


def _bot_uuid(n: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chaos-bot-{n}"))


async def _run_burst(section_id: str, volume: int) -> None:
    """Fire `volume` simulated registrations against `section_id`."""
    logger.info("Chaos burst starting: volume=%s section=%s", volume, section_id)
    await _ensure_chaos_students(section_id, volume)

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def worker(n: int) -> None:
        if _stop_event.is_set():
            return
        async with sem:
            if _stop_event.is_set():
                return
            _state.started += 1
            student_id = _bot_uuid(n)
            try:
                result = await registration.register(
                    student_id, section_id, endpoint_name="chaos"
                )
                outcome = result.outcome
                if outcome == "enrolled":
                    _state.enrolled += 1
                elif outcome == "waitlisted":
                    _state.waitlisted += 1
                elif outcome == "section full":
                    _state.section_full += 1
                elif outcome in (
                    "rate limited",
                    "prerequisites not met",
                    "invalid prerequisite graph",
                ):
                    _state.rejected += 1
                else:
                    _state.errors += 1
            except Exception:  # noqa: BLE001
                _state.errors += 1
                logger.debug("chaos bot %s failed", n, exc_info=True)

    try:
        await asyncio.gather(*(worker(i) for i in range(volume)))
    finally:
        _state.active = False
        logger.info("Chaos burst finished: %s", _state.summary())


async def start(volume: int, section_id: str) -> dict:
    """Validate config and launch a chaos burst (R8.1/8.6/8.7)."""
    if not isinstance(volume, int) or volume < MIN_VOLUME or volume > MAX_VOLUME:
        raise ChaosValidationError(
            f"volume must be an integer in [{MIN_VOLUME}, {MAX_VOLUME}]"
        )
    if not section_id:
        raise ChaosValidationError("section_id is required")
    if _state.active:
        raise ChaosAlreadyActive()

    # Reset counters and arm a fresh run.
    _stop_event.clear()
    _state.active = True
    _state.section_id = section_id
    _state.volume = volume
    _state.started = 0
    _state.enrolled = 0
    _state.waitlisted = 0
    _state.section_full = 0
    _state.rejected = 0
    _state.errors = 0

    _state.task = asyncio.create_task(
        _run_burst(section_id, volume), name="chaos-burst"
    )
    return _state.summary()


async def stop() -> dict:
    """Signal the running burst to cease generating new requests (R8.4)."""
    _stop_event.set()
    _state.active = False
    return _state.summary()
