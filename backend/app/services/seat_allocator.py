"""Seat_Allocator service wrapper (Requirements 1, 2, 12).

Invokes the `seat_lock_acquire` and `seat_lock_release` Lua scripts (loaded at
startup) via EVALSHA. The Lua scripts run atomically on the Redis server, so
concurrent acquires for the same section are serialized and the no-overselling
invariant holds at the Redis layer.

Redis keys per section (design.md "Redis Key Design"):
  KEYS[1] classq:seat:count:{sec}    available seat counter
  KEYS[2] classq:seat:locks:{sec}    hash student -> token:expires
  KEYS[3] classq:seat:lockexp:{sec}  zset student -> expires_ms
  KEYS[4] classq:waitlist:{sec}      zset student -> admission_ms (fair FIFO)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Literal

from app.core.config import settings
from app.db import redis

AcquireOutcome = Literal["OK", "WAITLISTED", "ALREADY_WAITLISTED", "FULL", "ALREADY_HELD"]
ReleaseMode = Literal["RELEASE", "CONFIRM"]


@dataclass
class AcquireResult:
    outcome: AcquireOutcome
    token: str | None = None
    position: int | None = None  # waitlist position for (ALREADY_)WAITLISTED


@dataclass
class ReleaseResult:
    outcome: str  # RELEASED | PROMOTED | CONFIRMED | NOOP
    promoted_student: str | None = None
    promoted_token: str | None = None


def _keys(section_id: str) -> list[str]:
    return [
        f"classq:seat:count:{section_id}",
        f"classq:seat:locks:{section_id}",
        f"classq:seat:lockexp:{section_id}",
        f"classq:waitlist:{section_id}",
    ]


async def acquire_lock(
    section_id: str,
    student_id: str,
    ttl: int = settings.seat_lock_ttl_seconds,
    *,
    max_waitlist_capacity: int = settings.max_waitlist_capacity,
) -> AcquireResult:
    """Attempt to acquire a Seat_Lock for `student_id` on `section_id`.

    Possible outcomes:
      OK                -> lock acquired; `token` is set.
      WAITLISTED        -> section full but added to waitlist; `position` set.
      ALREADY_WAITLISTED-> already on the waitlist; `position` set.
      FULL              -> section and waitlist both full.
      ALREADY_HELD      -> this student already holds a lock for this section.
    """
    client = redis.get_client()
    sha = redis.script_shas["seat_lock_acquire"]
    now_ms = int(time.time() * 1000)
    token = uuid.uuid4().hex
    ttl_ms = ttl * 1000

    keys = _keys(section_id)
    result = await client.evalsha(
        sha,
        len(keys),
        *keys,
        student_id,
        now_ms,
        token,
        ttl_ms,
        max_waitlist_capacity,
    )

    outcome = result[0]
    if outcome == "OK":
        return AcquireResult(outcome="OK", token=result[1])
    if outcome in ("WAITLISTED", "ALREADY_WAITLISTED"):
        position = int(result[1]) if len(result) > 1 else None
        return AcquireResult(outcome=outcome, position=position)
    # FULL or ALREADY_HELD
    return AcquireResult(outcome=outcome)


async def release_lock(
    section_id: str,
    student_id: str,
    token: str,
    mode: ReleaseMode = "RELEASE",
    *,
    ttl: int = settings.seat_lock_ttl_seconds,
) -> ReleaseResult:
    """Release or confirm a held Seat_Lock.

    mode="RELEASE" returns the seat to the pool (or auto-promotes the
    longest-waiting student on the waitlist). mode="CONFIRM" keeps the seat
    consumed because it has become a confirmed enrollment. Idempotent: a stale
    or already-released lock returns NOOP.
    """
    client = redis.get_client()
    sha = redis.script_shas["seat_lock_release"]
    now_ms = int(time.time() * 1000)
    ttl_ms = ttl * 1000
    promo_token = uuid.uuid4().hex  # pre-generated token for a promoted student

    keys = _keys(section_id)
    result = await client.evalsha(
        sha,
        len(keys),
        *keys,
        student_id,
        token,
        mode,
        now_ms,
        ttl_ms,
        promo_token,
    )

    outcome = result[0]
    if outcome == "PROMOTED":
        return ReleaseResult(
            outcome="PROMOTED",
            promoted_student=result[1] if len(result) > 1 else None,
            promoted_token=result[2] if len(result) > 2 else None,
        )
    return ReleaseResult(outcome=outcome)
