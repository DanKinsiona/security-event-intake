"""
Tests for the security event intake service.
Integration tests need a live Redis. They skip automatically if none is
reachable, so CI must provide one or they silently pass without testing
anything. They are marked `needs_redis`.
"""
from unittest.mock import patch

import pytest
import redis
from fastapi.testclient import TestClient

from app.main import app, r as redis_client, RATE_LIMIT_MAX, AUDIT_KEY

client = TestClient(app)


def _redis_available() -> bool:
    try:
        redis_client.ping()
        return True
    except redis.RedisError:
        return False


needs_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis is not reachable, integration tests skipped.",
)

# a valid event body reused across tests
VALID_EVENT = {
    "source": "auth-service",
    "event_type": "failed_login",
    "severity": "high",
    "message": "5 failed attempts from one IP",
}


def _clear_rate_limit(ip: str) -> None:
    # remove any existing rate-limit counters for this caller so the test
    # starts from a clean slate regardless of previous runs in the same window
    for key in redis_client.keys(f"ratelimit:{ip}:*"):
        redis_client.delete(key)


def _clear_audit() -> None:
    redis_client.delete(AUDIT_KEY)


# ---------- unit-ish tests (no Redis required) ----------

def test_healthz_returns_503_when_redis_down():
    with patch.object(redis_client, "ping", side_effect=redis.ConnectionError("nope")):
        resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["redis"] == "unreachable"


def test_create_event_rejects_empty_source():
    body = {**VALID_EVENT, "source": ""}
    resp = client.post("/events", json=body)
    assert resp.status_code == 422


def test_create_event_rejects_bad_severity():
    body = {**VALID_EVENT, "severity": "banana"}
    resp = client.post("/events", json=body)
    assert resp.status_code == 422


def test_create_event_rejects_missing_event_type():
    body = {k: v for k, v in VALID_EVENT.items() if k != "event_type"}
    resp = client.post("/events", json=body)
    assert resp.status_code == 422


# ---------- integration tests (need Redis) ----------

@needs_redis
def test_healthz_ok_when_redis_up():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "redis": "ok"}


@needs_redis
def test_create_and_fetch_event_roundtrip():
    create = client.post("/events", json=VALID_EVENT)
    assert create.status_code == 201
    event_id = create.json()["event_id"]
    assert len(event_id) == 32
    assert create.json()["received_at"]  # server stamped a timestamp

    fetched = client.get(f"/events/{event_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["source"] == "auth-service"
    assert body["event_type"] == "failed_login"
    assert body["severity"] == "high"
    assert body["message"] == "5 failed attempts from one IP"


@needs_redis
def test_event_without_message_is_allowed():
    body = {k: v for k, v in VALID_EVENT.items() if k != "message"}
    create = client.post("/events", json=body)
    assert create.status_code == 201
    fetched = client.get(f"/events/{create.json()['event_id']}")
    assert fetched.json()["message"] is None


@needs_redis
def test_unknown_event_returns_404():
    resp = client.get("/events/does-not-exist")
    assert resp.status_code == 404


@needs_redis
def test_rate_limit_blocks_after_max():
    # unique caller (a documentation-reserved IP) with its own counter
    ip = "203.0.113.99"
    _clear_rate_limit(ip)
    headers = {"X-Forwarded-For": ip}

    # requests up to the limit all succeed
    for _ in range(RATE_LIMIT_MAX):
        resp = client.post("/events", json=VALID_EVENT, headers=headers)
        assert resp.status_code == 201

    # the next one is over the limit
    resp = client.post("/events", json=VALID_EVENT, headers=headers)
    assert resp.status_code == 429


@needs_redis
def test_rate_limit_is_per_caller():
    # one caller exhausts its limit
    noisy = "203.0.113.1"
    quiet = "203.0.113.2"
    _clear_rate_limit(noisy)
    _clear_rate_limit(quiet)

    for _ in range(RATE_LIMIT_MAX + 1):
        client.post("/events", json=VALID_EVENT, headers={"X-Forwarded-For": noisy})

    # a different caller is unaffected: the limit is per IP, not global
    resp = client.post("/events", json=VALID_EVENT, headers={"X-Forwarded-For": quiet})
    assert resp.status_code == 201


@needs_redis
def test_audit_records_event_creation():
    ip = "203.0.113.50"
    _clear_rate_limit(ip)
    _clear_audit()

    create = client.post("/events", json=VALID_EVENT, headers={"X-Forwarded-For": ip})
    assert create.status_code == 201
    event_id = create.json()["event_id"]

    resp = client.get("/audit")
    assert resp.status_code == 200
    entries = resp.json()
    # the creation we just made is recorded, tagged with its event_id
    assert any(
        e["action"] == "event_created" and e.get("event_id") == event_id
        for e in entries
    )


@needs_redis
def test_audit_records_rate_limited_rejection():
    ip = "203.0.113.51"
    _clear_rate_limit(ip)
    _clear_audit()

    # push past the limit so at least one request is rejected
    for _ in range(RATE_LIMIT_MAX + 1):
        client.post("/events", json=VALID_EVENT, headers={"X-Forwarded-For": ip})

    entries = client.get("/audit").json()
    assert any(e["action"] == "rate_limited" and e["ip"] == ip for e in entries)
