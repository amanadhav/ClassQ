"""Async Redis connection management and Lua script registration.

On startup we open a shared async Redis client and load the three core Lua
scripts (verbatim from .kiro/specs/classq-course-registration/design.md) into
the server's script cache via SCRIPT LOAD. The returned SHA1 hashes are kept in
`script_shas` so callers can invoke them with EVALSHA later.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import settings

# --- Core Lua scripts (verbatim from design.md) ----------------------------

# Seat Lock Acquire
# (Requirements 1.1, 1.2, 1.5, 2.1, 2.2, 2.6, 2.7, 12.1, 12.5, 12.8, 12.9)
SEAT_LOCK_ACQUIRE = """
-- KEYS[1] = classq:seat:count:{sec}
-- KEYS[2] = classq:seat:locks:{sec}   (hash student -> token:expires)
-- KEYS[3] = classq:seat:lockexp:{sec} (zset student -> expires_ms)
-- KEYS[4] = classq:waitlist:{sec}     (zset student -> admission_ms, fair FIFO)
-- ARGV[1] = student_id
-- ARGV[2] = now_ms
-- ARGV[3] = lock_token
-- ARGV[4] = ttl_ms
-- ARGV[5] = max_waitlist_capacity     (Maximum_Waitlist_Capacity, default 50)
-- Returns: {"OK", token}
--        | {"WAITLISTED", position}        (added to tail of waitlist; position = ZRANK+1) (R12.1, R12.8)
--        | {"ALREADY_WAITLISTED", position}(already on waitlist; position unchanged)        (R12.5)
--        | {"FULL"}                        (no seat AND waitlist at capacity)               (R12.4)
--        | {"ALREADY_HELD"}

-- 1. Reject if this student already holds a lock for this section (R2.7)
if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 1 then
  return {'ALREADY_HELD'}
end

-- 2. Lazily reclaim expired locks so their seats are available (R2.4)
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[2])
for _, stu in ipairs(expired) do
  redis.call('HDEL', KEYS[2], stu)
  redis.call('ZREM', KEYS[3], stu)
  redis.call('INCR', KEYS[1])          -- return reclaimed seat to pool
end

-- 3. Check availability. When no seat is free, route to the fair FIFO waitlist
--    instead of immediately failing (R1.2, R1.5, R2.6, R12.1).
local avail = tonumber(redis.call('GET', KEYS[1]) or '0')
if avail <= 0 then
  -- 3a. Idempotent membership: already waitlisted -> return existing position (R12.5, R12.9)
  if redis.call('ZSCORE', KEYS[4], ARGV[1]) then
    local rank = redis.call('ZRANK', KEYS[4], ARGV[1])
    return {'ALREADY_WAITLISTED', rank + 1}
  end
  -- 3b. Room on the waitlist -> append at the tail with admission timestamp (R12.1, R12.8)
  if redis.call('ZCARD', KEYS[4]) < tonumber(ARGV[5]) then
    redis.call('ZADD', KEYS[4], tonumber(ARGV[2]), ARGV[1])
    local rank = redis.call('ZRANK', KEYS[4], ARGV[1])
    return {'WAITLISTED', rank + 1}
  end
  -- 3c. Waitlist itself is at capacity -> section full (R2.7, R12.4)
  return {'FULL'}
end

-- 4. Atomically take one seat and record the exclusive lock (R1.1, R2.1, R2.2)
redis.call('DECR', KEYS[1])
local expires = tonumber(ARGV[2]) + tonumber(ARGV[4])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[3] .. ':' .. expires)
redis.call('ZADD', KEYS[3], expires, ARGV[1])
return {'OK', ARGV[3]}
""".strip()

# Seat Lock Release / Auto-Promotion
# (Requirements 2.3, 2.5, 1.6, 12.2, 12.3, 12.6)
SEAT_LOCK_RELEASE = """
-- KEYS[1] = classq:seat:count:{sec}
-- KEYS[2] = classq:seat:locks:{sec}
-- KEYS[3] = classq:seat:lockexp:{sec}
-- KEYS[4] = classq:waitlist:{sec}     (zset student -> admission_ms, fair FIFO)
-- ARGV[1] = student_id
-- ARGV[2] = lock_token
-- ARGV[3] = mode   ("RELEASE" returns seat | "CONFIRM" keeps seat consumed)
-- ARGV[4] = now_ms
-- ARGV[5] = ttl_ms                    (lock TTL for a promoted student)
-- ARGV[6] = promo_lock_token          (pre-generated token for the promoted student)
-- Returns: {"RELEASED"}                       (seat returned to open pool; waitlist empty)
--        | {"PROMOTED", promoted_student, token}(freed seat handed to longest-waiting student) (R12.2)
--        | {"CONFIRMED"}                       (seat stays consumed; becomes enrollment) (R2.3)
--        | {"NOOP"}                            (idempotent, releases at most once) (R2.5)

local cur = redis.call('HGET', KEYS[2], ARGV[1])
if not cur then
  return {'NOOP'}                       -- already released/converted (R2.5 exactly-once)
end
local token = string.match(cur, '([^:]+):')
if token ~= ARGV[2] then
  return {'NOOP'}                       -- stale token, do nothing
end

-- Release the caller's lock state regardless of mode.
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])

if ARGV[3] == 'RELEASE' then
  -- Auto-promotion: if anyone is waiting, the freed seat goes to the
  -- longest-waiting student (lowest admission score) rather than back to the
  -- open pool, so the seat is never double-counted (R12.2, R12.3, R12.6).
  local head = redis.call('ZRANGE', KEYS[4], 0, 0)
  if head and head[1] then
    local promoted = head[1]
    redis.call('ZREM', KEYS[4], promoted)            -- leave waitlist atomically (R12.6)
    local expires = tonumber(ARGV[4]) + tonumber(ARGV[5])
    redis.call('HSET', KEYS[2], promoted, ARGV[6] .. ':' .. expires)  -- new Seat_Lock
    redis.call('ZADD', KEYS[3], expires, promoted)
    -- NOTE: seat:count is intentionally NOT incremented; the seat moves
    -- directly from the releasing holder to the promoted holder.
    return {'PROMOTED', promoted, ARGV[6]}
  end
  redis.call('INCR', KEYS[1])           -- waitlist empty -> return seat to pool (R2.5, R1.6)
  return {'RELEASED'}
else
  return {'CONFIRMED'}                   -- seat stays consumed; becomes enrollment (R2.3)
end
""".strip()

# Sliding-Window Rate Limit
# (Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6)
SLIDING_WINDOW_RATE_LIMIT = """
-- KEYS[1] = classq:rl:stu:{stu}:{endpoint}
-- KEYS[2] = classq:rl:ep:{endpoint}
-- ARGV[1] = now_ms
-- ARGV[2] = window_ms
-- ARGV[3] = student_limit
-- ARGV[4] = endpoint_limit   (-1 if endpoint not configured)
-- ARGV[5] = request_id
-- Returns: {"OK"} | {"LIMITED", retry_after_seconds}

local window_start = tonumber(ARGV[1]) - tonumber(ARGV[2])

-- Evict entries older than the window (R3.5)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', window_start)
if tonumber(ARGV[4]) >= 0 then
  redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', window_start)
end

local stu_count = redis.call('ZCARD', KEYS[1])
local ep_count  = (tonumber(ARGV[4]) >= 0) and redis.call('ZCARD', KEYS[2]) or 0

-- Would accepting exceed either limit? (R3.2, R3.4)
local over_stu = (stu_count + 1) > tonumber(ARGV[3])
local over_ep  = (tonumber(ARGV[4]) >= 0) and ((ep_count + 1) > tonumber(ARGV[4]))
if over_stu or over_ep then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_after = 0
  if oldest[2] then
    retry_after = math.ceil((tonumber(oldest[2]) + tonumber(ARGV[2]) - tonumber(ARGV[1])) / 1000)
  end
  return {'LIMITED', retry_after}
end

-- Record this request and allow it (R3.6) -- whole evaluation is atomic (R3.3)
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[5])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
if tonumber(ARGV[4]) >= 0 then
  redis.call('ZADD', KEYS[2], ARGV[1], ARGV[5])
  redis.call('PEXPIRE', KEYS[2], ARGV[2])
end
return {'OK'}
""".strip()

# Logical name -> source mapping used during SCRIPT LOAD.
_SCRIPTS: dict[str, str] = {
    "seat_lock_acquire": SEAT_LOCK_ACQUIRE,
    "seat_lock_release": SEAT_LOCK_RELEASE,
    "sliding_window_rate_limit": SLIDING_WINDOW_RATE_LIMIT,
}

# Populated at startup: logical name -> SHA1 hash returned by SCRIPT LOAD.
script_shas: dict[str, str] = {}

_client: aioredis.Redis | None = None


async def connect() -> aioredis.Redis:
    """Create the shared async Redis client and register Lua scripts."""
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await load_scripts()
    return _client


async def load_scripts() -> dict[str, str]:
    """Inject the core Lua scripts via SCRIPT LOAD and cache their SHA1 hashes."""
    client = get_client()
    for name, source in _SCRIPTS.items():
        sha = await client.script_load(source)
        script_shas[name] = sha
    return script_shas


async def disconnect() -> None:
    """Close the shared Redis client."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        script_shas.clear()


def get_client() -> aioredis.Redis:
    """Return the shared Redis client, raising if not initialised."""
    if _client is None:
        raise RuntimeError("Redis client is not initialised; call connect() first.")
    return _client


async def ping() -> bool:
    """Return True if Redis responds to PING."""
    client = get_client()
    return bool(await client.ping())
