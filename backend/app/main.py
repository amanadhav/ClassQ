"""ClassQ FastAPI application entry point.

Manages the lifecycle of the PostgreSQL pool and Redis client (including Lua
script registration) and exposes a /health endpoint that pings both stores.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import postgres, redis
from app.services import chaos, metrics, prerequisite, registration
from app.workers import outbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("classq")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    await postgres.connect()
    await redis.connect()

    # Background workers (R6 outbox processor, R7 metrics broadcaster).
    outbox_task = asyncio.create_task(outbox.process_outbox(), name="outbox-processor")
    metrics_task = asyncio.create_task(
        metrics.broadcast_metrics(), name="metrics-broadcaster"
    )

    logger.info(
        "ClassQ startup complete: postgres pool ready, redis connected, "
        "lua scripts loaded=%s, background tasks started",
        list(redis.script_shas.keys()),
    )
    try:
        yield
    finally:
        # --- Shutdown ---
        for task in (outbox_task, metrics_task):
            task.cancel()
        for task in (outbox_task, metrics_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await redis.disconnect()
        await postgres.disconnect()
        logger.info("ClassQ shutdown complete: connections closed")


app = FastAPI(title="ClassQ", version="0.1.0", lifespan=lifespan)

# Allow the Vite dev server (and production frontend) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000",
                   "http://classq-prod-alb-1896872101.us-east-1.elb.amazonaws.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> JSONResponse:
    """Asynchronously ping Postgres and Redis and report connection status."""
    components: dict[str, str] = {}

    try:
        postgres_ok = await postgres.ping()
    except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
        postgres_ok = False
        logger.warning("Postgres health check failed: %s", exc)
    components["postgres"] = "ok" if postgres_ok else "unavailable"

    try:
        redis_ok = await redis.ping()
    except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
        redis_ok = False
        logger.warning("Redis health check failed: %s", exc)
    components["redis"] = "ok" if redis_ok else "unavailable"

    healthy = postgres_ok and redis_ok
    body = {
        "status": "healthy" if healthy else "unhealthy",
        "components": components,
        "lua_scripts": sorted(redis.script_shas.keys()),
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)


@app.get("/test/prereq/{student_id}/{course_id}")
async def test_prereq(student_id: str, course_id: str) -> JSONResponse:
    """TEMPORARY validation route for the Prerequisite_Checker (Phase 4a).

    Invokes the prerequisite service and returns the evaluation result
    (satisfied boolean, outcome, any unmet courses, and whether it was a cache
    hit). Remove once registration endpoints are implemented.
    """
    result = await prerequisite.evaluate_prerequisites(student_id, course_id)
    status_code = 200 if result.outcome in ("satisfied", "unmet") else 422
    return JSONResponse(status_code=status_code, content=result.to_dict())


class RegisterRequest(BaseModel):
    section_id: str


# Maps registration outcomes to HTTP status codes (R11).
_OUTCOME_STATUS = {
    "enrolled": 201,
    "waitlisted": 202,
    "section full": 409,
    "already enrolled": 409,
    "prerequisites not met": 400,
    "invalid prerequisite graph": 400,
    "rate limited": 429,
    "error": 500,
}


@app.post("/register")
async def register(
    body: RegisterRequest,
    x_student_id: str | None = Header(default=None, alias="X-Student-ID"),
) -> JSONResponse:
    """Register a student for a section.

    Dummy auth for now: the student identity is taken from the X-Student-ID
    header. Real authentication (R10) is implemented in a later phase.
    """
    if not x_student_id:
        raise HTTPException(status_code=401, detail="X-Student-ID header required")

    result = await registration.register(x_student_id, body.section_id)
    status_code = _OUTCOME_STATUS.get(result.outcome, 200)
    return JSONResponse(status_code=status_code, content=result.to_dict())


class ChaosStartRequest(BaseModel):
    volume: int = 500
    section_id: str


@app.post("/chaos/start")
async def chaos_start(body: ChaosStartRequest) -> JSONResponse:
    """Launch a chaos burst of simulated registrations (R8)."""
    try:
        summary = await chaos.start(body.volume, body.section_id)
    except chaos.ChaosValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except chaos.ChaosAlreadyActive:
        raise HTTPException(status_code=409, detail="simulation already active")
    return JSONResponse(status_code=202, content=summary)


@app.post("/chaos/stop")
async def chaos_stop() -> JSONResponse:
    """Signal a running chaos burst to stop generating new requests (R8.4)."""
    summary = await chaos.stop()
    return JSONResponse(status_code=200, content=summary)


@app.get("/chaos/status")
async def chaos_status() -> JSONResponse:
    """Return the current/last chaos burst summary."""
    return JSONResponse(status_code=200, content=chaos.status())


@app.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket) -> None:
    """Live metrics WebSocket (R7).

    Registers the connection with the MetricsManager, which fans out a batched
    heartbeat every 500 ms. Rejects with policy-violation code 1008 when the
    100-connection cap is reached (R7.7).
    """
    try:
        await metrics.manager.connect(websocket)
    except metrics.ConnectionLimitReached:
        await websocket.close(code=1008, reason="connection limit reached")
        return

    try:
        # Keep the connection open; broadcasts are pushed by the background task.
        # We await client messages only to detect disconnects.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await metrics.manager.disconnect(websocket)
