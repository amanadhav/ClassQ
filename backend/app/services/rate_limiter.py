"""Rate_Limiter service wrapper (Requirement 3).

Invokes the `sliding_window_rate_limit` Lua script (loaded at startup) via
EVALSHA. The script atomically evicts expired entries, counts requests in the
sliding window, and either records+allows the request or rejects it with a
retry-after value.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from app.core.config import settings
from app.db import redis

# Defaults from requirements (R3.1 default 60s window, R3.2 default 100 req/window).
DEFAULT_WINDOW_MS = settings.rate_limit_window_ms
DEFAULT_STUDENT_LIMIT = settings.rate_limit_student_limit


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0


def _stu_key(student_id: str, endpoint: str) -> str:
    return f"classq:rl:stu:{student_id}:{endpoint}"


def _ep_key(endpoint: str) -> str:
    return f"classq:rl:ep:{endpoint}"


async def check(
    student_id: str,
    endpoint: str,
    *,
    window_ms: int = DEFAULT_WINDOW_MS,
    student_limit: int = DEFAULT_STUDENT_LIMIT,
    endpoint_limit: int = -1,
) -> RateLimitResult:
    """Evaluate the sliding-window rate limit for (student, endpoint).

    Returns RateLimitResult(allowed=False, retry_after_seconds=N) when limited.
    """
    client = redis.get_client()
    sha = redis.script_shas["sliding_window_rate_limit"]
    now_ms = int(time.time() * 1000)
    request_id = uuid.uuid4().hex

    result = await client.evalsha(
        sha,
        2,  # number of KEYS
        _stu_key(student_id, endpoint),
        _ep_key(endpoint),
        now_ms,
        window_ms,
        student_limit,
        endpoint_limit,
        request_id,
    )

    status = result[0]
    if status == "OK":
        return RateLimitResult(allowed=True)
    # status == "LIMITED"
    retry_after = int(result[1]) if len(result) > 1 else 0
    return RateLimitResult(allowed=False, retry_after_seconds=retry_after)
