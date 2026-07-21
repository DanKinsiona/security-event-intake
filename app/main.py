"""
Security event intake service.
A small front door that other systems report security-relevant events to:
a failed login, a config change, a finished scan. Clients POST a structured
event and we hand back an event ID they can fetch later. The service only
validates, records, and returns events; it does not act on them.

Security features:
- The intake endpoint is rate limited per client IP (a first, cheap layer of
  abuse protection).
- An audit trail records the service's own activity: every accepted event and
  every rate-limited rejection, with a timestamp and the caller IP.

State (events, rate-limit counters, and the audit log) lives in Redis.

NOTE: /audit exposes caller IPs and activity, so it is sensitive. In a real
deployment it must be behind authentication / admin-only access. It is left
open here for demonstration and flagged as a next step.
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import redis
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

logger = logging.getLogger("event_intake")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD: Optional[str] = os.getenv("REDIS_PASSWORD") or None
EVENT_TTL_SECONDS = int(os.getenv("EVENT_TTL_SECONDS", "86400"))  # keep events 24h by default

# Rate limit config: defaulted low so it's easy to demonstrate, tunable per env.
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# Audit log config: a capped Redis list of recent activity.
AUDIT_KEY = "audit:log"
AUDIT_MAX_ENTRIES = int(os.getenv("AUDIT_MAX_ENTRIES", "1000"))

app = FastAPI(title="Security Event Intake", version="0.3.0")

# Module-level client. socket_connect_timeout keeps the healthcheck
# snappy when Redis is unreachable instead of hanging the worker.
r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)

Severity = Literal["low", "medium", "high", "critical"]


class EventRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=128)
    event_type: str = Field(..., min_length=1, max_length=64)
    severity: Severity
    message: Optional[str] = Field(None, max_length=512)


class EventResponse(BaseModel):
    event_id: str
    source: str
    event_type: str
    severity: Severity
    message: Optional[str]
    received_at: str


def _event_key(event_id: str) -> str:
    return f"event:{event_id}"


def _client_ip(request: Request) -> str:
    # nginx sets X-Forwarded-For to the real client IP. It can be a
    # comma-separated list (client, proxy1, ...); the first entry is the
    # original client. Fall back to the direct socket IP if the header is
    # absent (e.g. hitting the app without the proxy, as the tests do).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _record_audit(ip: str, action: str, **extra) -> None:
    """
    Append one entry to the audit log. Fails open: an audit write problem
    should never break the request being audited.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "action": action,
        **extra,
    }
    try:
        r.lpush(AUDIT_KEY, json.dumps(entry))          # newest first
        r.ltrim(AUDIT_KEY, 0, AUDIT_MAX_ENTRIES - 1)   # cap the log length
    except redis.RedisError as exc:
        logger.warning("Failed to write audit entry: %s", exc)


def _enforce_rate_limit(ip: str) -> None:
    """
    Fixed-window rate limit, one counter per client IP per time window,
    stored in Redis so the limit stays correct across multiple API replicas.
    Fails open: if Redis is unreachable the request is allowed, because the
    limiter should not take the service down (and storage will 503 anyway).
    """
    now = int(time.time())
    window_start = now - (now % RATE_LIMIT_WINDOW_SECONDS)  # clock-aligned window
    key = f"ratelimit:{ip}:{window_start}"
    try:
        count = r.incr(key)
        if count == 1:
            # first hit in this window: set the key to expire when the window ends
            r.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    except redis.RedisError as exc:
        logger.warning("Rate limit check failed, allowing request: %s", exc)
        return
    if count > RATE_LIMIT_MAX:
        logger.info("Rate limit exceeded for %s (%s in window)", ip, count)
        _record_audit(ip, "rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
        )


@app.get("/healthz")
def healthz(response: Response) -> dict:
    """
    Liveness + readiness in one. Returns 503 if Redis is unreachable
    so an orchestrator can route traffic away.
    """
    try:
        r.ping()
    except redis.RedisError as exc:
        logger.warning("Redis ping failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "redis": "unreachable"}
    return {"status": "ok", "redis": "ok"}


@app.post("/events", status_code=status.HTTP_201_CREATED, response_model=EventResponse)
def create_event(req: EventRequest, request: Request) -> EventResponse:
    ip = _client_ip(request)
    _enforce_rate_limit(ip)  # reject over-limit callers before doing any work
    event_id = uuid.uuid4().hex
    received_at = datetime.now(timezone.utc).isoformat()  # server stamps the time, not the client
    payload = {
        "event_id": event_id,
        "source": req.source,
        "event_type": req.event_type,
        "severity": req.severity,
        "message": req.message or "",
        "received_at": received_at,
    }
    try:
        r.hset(_event_key(event_id), mapping=payload)
        r.expire(_event_key(event_id), EVENT_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.error("Failed to persist event %s: %s", event_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable",
        )
    _record_audit(ip, "event_created", event_id=event_id, severity=req.severity)
    return EventResponse(
        event_id=event_id,
        source=req.source,
        event_type=req.event_type,
        severity=req.severity,
        message=req.message,
        received_at=received_at,
    )


@app.get("/events/{event_id}", response_model=EventResponse)
def get_event(event_id: str) -> EventResponse:
    try:
        data = r.hgetall(_event_key(event_id))
    except redis.RedisError as exc:
        logger.error("Failed to read event %s: %s", event_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable",
        )
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    return EventResponse(
        event_id=data["event_id"],
        source=data["source"],
        event_type=data["event_type"],
        severity=data["severity"],
        message=data["message"] or None,
        received_at=data["received_at"],
    )


@app.get("/audit")
def get_audit(limit: int = 50) -> list:
    """
    Return the most recent audit entries, newest first.
    Sensitive (exposes caller IPs); would be admin-only in production.
    """
    limit = max(1, min(limit, 200))  # clamp so a caller can't ask for everything
    try:
        raw = r.lrange(AUDIT_KEY, 0, limit - 1)
    except redis.RedisError as exc:
        logger.error("Failed to read audit log: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable",
        )
    return [json.loads(entry) for entry in raw]
