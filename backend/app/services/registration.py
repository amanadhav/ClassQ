"""Registration_Service orchestrator (Requirements 1, 2, 3, 4, 6, 11, 12).

Coordinates the registration pipeline:
  1. Rate limit         (Rate_Limiter, R3)
  2. Prerequisite check (Prerequisite_Checker, R4)
  3. Seat lock acquire  (Seat_Allocator, R1/R2/R12)
  4. Durable commit     (enrollment + counter + outbox in ONE transaction, R1/R6)

The Step-4 transaction is the critical section for the no-overselling
invariant: the counter UPDATE is guarded by `confirmed_count < capacity`, and a
zero-row update (or any constraint violation) rolls the whole thing back and
releases the Redis seat lock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import settings
from app.db import postgres, redis
from app.services import metrics, prerequisite, rate_limiter, seat_allocator

logger = logging.getLogger("classq")

RegistrationOutcome = Literal[
    "enrolled",
    "waitlisted",
    "section full",
    "prerequisites not met",
    "rate limited",
    "invalid prerequisite graph",
    "already enrolled",
    "error",
]


@dataclass
class RegistrationResult:
    outcome: RegistrationOutcome
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, **self.detail}


class RegistrationError(Exception):
    """Raised when the critical transaction cannot complete safely."""


async def _load_section(conn, section_id: str):
    """Return (course_id, capacity, confirmed_count) for a section, or None."""
    return await conn.fetchrow(
        """
        SELECT course_id, capacity, confirmed_count
        FROM course_sections
        WHERE section_id = $1::uuid
        """,
        section_id,
    )


async def _ensure_seat_counter(section_id: str, available: int) -> None:
    """Initialise the Redis seat counter for a section if not already present.

    Uses SET NX so repeated registrations never clobber the live counter. The
    counter is seeded from (capacity - confirmed_count) so Redis and the durable
    DB count start in agreement. (In production this warm-up happens when a
    registration window opens; we do it lazily here.)
    """
    client = redis.get_client()
    await client.set(f"classq:seat:count:{section_id}", available, nx=True)


async def register(
    student_id: str,
    section_id: str,
    endpoint_name: str = "register",
) -> RegistrationResult:
    """Run the full registration pipeline for one (student, section) request."""

    # --- Step 1: Rate limit (R3) ---
    rl = await rate_limiter.check(student_id, endpoint_name)
    if not rl.allowed:
        return RegistrationResult(
            outcome="rate limited",
            detail={"retry_after_seconds": rl.retry_after_seconds},
        )

    # Resolve the section -> course (needed for the prerequisite check) and
    # warm the Redis seat counter from the durable capacity.
    pool = postgres.get_pool()
    async with pool.acquire() as conn:
        section = await _load_section(conn, section_id)
    if section is None:
        return RegistrationResult(
            outcome="error", detail={"reason": "unknown section"}
        )

    course_id = str(section["course_id"])
    available = int(section["capacity"]) - int(section["confirmed_count"])
    await _ensure_seat_counter(section_id, available)

    # --- Step 2: Prerequisite check (R4) ---
    prereq = await prerequisite.evaluate_prerequisites(student_id, course_id)
    if prereq.outcome == "unmet":
        return RegistrationResult(
            outcome="prerequisites not met", detail={"unmet": prereq.unmet}
        )
    if prereq.outcome == "invalid":
        return RegistrationResult(outcome="invalid prerequisite graph")
    if prereq.outcome == "error":
        return RegistrationResult(
            outcome="error", detail={"reason": "prerequisite evaluation failed"}
        )

    # --- Step 3: Acquire seat lock (R1/R2/R12) ---
    acq = await seat_allocator.acquire_lock(section_id, student_id)
    if acq.outcome == "FULL":
        return RegistrationResult(outcome="section full")
    if acq.outcome in ("WAITLISTED", "ALREADY_WAITLISTED"):
        # Waitlist DB persistence is handled in a later phase; surface the
        # Redis-level outcome and position for now.
        return RegistrationResult(
            outcome="waitlisted", detail={"position": acq.position}
        )
    if acq.outcome == "ALREADY_HELD":
        return RegistrationResult(
            outcome="error",
            detail={"reason": "a registration for this section is already in progress"},
        )

    # acq.outcome == "OK": we hold the lock token.
    token = acq.token
    assert token is not None

    # --- Step 4: Critical durable transaction (R1/R6) ---
    try:
        enrollment_id = await _commit_enrollment(student_id, section_id)
    except Exception as exc:  # rollback already happened inside; release the seat
        await seat_allocator.release_lock(section_id, student_id, token, mode="RELEASE")
        logger.warning(
            "Registration transaction failed for student=%s section=%s: %s",
            student_id,
            section_id,
            exc,
        )
        if isinstance(exc, RegistrationError):
            return RegistrationResult(outcome="section full")
        return RegistrationResult(
            outcome="error", detail={"reason": "registration transaction failed"}
        )

    # Success: confirm the lock so the seat stays consumed (becomes enrollment).
    await seat_allocator.release_lock(section_id, student_id, token, mode="CONFIRM")
    await metrics.record_allocation()  # feed allocations_per_sec (R7.3)
    return RegistrationResult(
        outcome="enrolled", detail={"enrollment_id": enrollment_id}
    )


async def _commit_enrollment(student_id: str, section_id: str) -> str:
    """Insert enrollment + increment counter + write outbox in ONE transaction.

    Raises RegistrationError if the guarded counter UPDATE affects zero rows
    (section is durably full); the transaction is rolled back in that case.
    Returns the new enrollment_id on success.
    """
    pool = postgres.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            enrollment_id = await conn.fetchval(
                """
                INSERT INTO enrollments (student_id, section_id, status)
                VALUES ($1::uuid, $2::uuid, 'confirmed')
                RETURNING enrollment_id
                """,
                student_id,
                section_id,
            )

            # Guarded increment — the heart of the no-overselling invariant.
            updated = await conn.execute(
                """
                UPDATE course_sections
                SET confirmed_count = confirmed_count + 1
                WHERE section_id = $1::uuid AND confirmed_count < capacity
                """,
                section_id,
            )
            # asyncpg returns a tag like "UPDATE 1"; 0 rows means the section is full.
            if updated.split()[-1] == "0":
                raise RegistrationError("section is full (guarded update affected 0 rows)")

            await conn.execute(
                """
                INSERT INTO outbox (section_id, event_type, payload)
                VALUES ($1::uuid, $2, $3::jsonb)
                """,
                section_id,
                "enrollment.confirmed",
                json.dumps(
                    {
                        "enrollment_id": str(enrollment_id),
                        "student_id": student_id,
                        "section_id": section_id,
                    }
                ),
            )

    return str(enrollment_id)
