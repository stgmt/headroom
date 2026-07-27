"""Long-lived streaming recovery for transient upstream unavailability.

Claude Code treats an HTTP 429 as a failed agent turn.  A subscription reset can
be tens of minutes away, so a handful of ordinary retries is not enough.  This
module keeps the Anthropic SSE connection alive with protocol-valid ping events,
waits for Retry-After, and serializes probe requests across all affected callers.
The same held-stream path also survives bounded gateway outages and transport
disconnects without hiding non-retryable request or authentication errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from headroom.proxy.helpers import retry_after_ms

logger = logging.getLogger("headroom.proxy")

_PING_EVENT = b'event: ping\ndata: {"type":"ping"}\n\n'
_JSON_KEEPALIVE = b" \n"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_statuses(name: str, default: frozenset[int]) -> frozenset[int]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    statuses: set[int] = set()
    for value in raw.split(","):
        try:
            status = int(value.strip())
        except ValueError:
            continue
        if 400 <= status <= 599:
            statuses.add(status)
    return frozenset(statuses) or default


@dataclass(frozen=True)
class UpstreamCooldownPolicy:
    """Runtime policy for the streaming upstream-recovery hold path."""

    enabled: bool
    max_wait_seconds: float
    heartbeat_seconds: float
    default_retry_seconds: float
    hold_statuses: frozenset[int] = frozenset({429, 502, 503, 504, 529})

    @classmethod
    def from_env(cls) -> UpstreamCooldownPolicy:
        return cls(
            enabled=_env_bool("HEADROOM_UPSTREAM_429_HOLD_ENABLED", False),
            max_wait_seconds=_env_float(
                "HEADROOM_UPSTREAM_429_MAX_WAIT_SECONDS", 21600.0, minimum=1.0
            ),
            heartbeat_seconds=_env_float(
                "HEADROOM_UPSTREAM_429_HEARTBEAT_SECONDS", 15.0, minimum=0.05
            ),
            default_retry_seconds=_env_float(
                "HEADROOM_UPSTREAM_429_DEFAULT_RETRY_SECONDS", 30.0, minimum=0.05
            ),
            hold_statuses=_env_statuses(
                "HEADROOM_UPSTREAM_RECOVERY_HOLD_STATUSES",
                frozenset({429, 502, 503, 504, 529}),
            ),
        )


class UpstreamCooldownGate:
    """One event-loop-local cooldown clock and probe lock per upstream route."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._until: dict[str, float] = {}
        self._probe_locks: dict[str, asyncio.Lock] = {}
        self._active_holds = 0
        self._holds_total = 0
        self._recoveries_total = 0
        self._timeouts_total = 0
        self._cancellations_total = 0
        self._transport_failures_total = 0

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is self._loop:
            return
        self._loop = loop
        self._until = {}
        self._probe_locks = {}

    def remaining(self, key: str) -> float:
        self._ensure_loop()
        return max(0.0, self._until.get(key, 0.0) - time.monotonic())

    def defer(self, key: str, delay_seconds: float) -> float:
        self._ensure_loop()
        retry_at = time.monotonic() + max(0.0, delay_seconds)
        self._until[key] = max(self._until.get(key, 0.0), retry_at)
        return self.remaining(key)

    def clear(self, key: str) -> None:
        self._ensure_loop()
        self._until.pop(key, None)

    def probe_lock(self, key: str) -> asyncio.Lock:
        self._ensure_loop()
        return self._probe_locks.setdefault(key, asyncio.Lock())

    def hold_started(self) -> None:
        self._active_holds += 1
        self._holds_total += 1

    def hold_finished(self, outcome: str) -> None:
        self._active_holds = max(0, self._active_holds - 1)
        if outcome == "recovered":
            self._recoveries_total += 1
        elif outcome == "timeout":
            self._timeouts_total += 1
        elif outcome == "cancelled":
            self._cancellations_total += 1

    def record_transport_failure(self) -> None:
        self._transport_failures_total += 1

    def snapshot(self) -> dict[str, int | float]:
        self._ensure_loop()
        now = time.monotonic()
        remaining = [max(0.0, value - now) for value in self._until.values()]
        cooling = [value for value in remaining if value > 0]
        return {
            "active_holds": self._active_holds,
            "cooling_routes": len(cooling),
            "next_probe_seconds": min(cooling) if cooling else 0.0,
            "holds_total": self._holds_total,
            "recoveries_total": self._recoveries_total,
            "timeouts_total": self._timeouts_total,
            "cancellations_total": self._cancellations_total,
            "transport_failures_total": self._transport_failures_total,
        }


def get_upstream_cooldown_gate(owner: Any) -> UpstreamCooldownGate:
    """Return a per-proxy gate without requiring every test double to initialize it."""

    gate = getattr(owner, "_upstream_cooldown_gate", None)
    if not isinstance(gate, UpstreamCooldownGate):
        gate = UpstreamCooldownGate()
        owner._upstream_cooldown_gate = gate
    return gate


def upstream_route_key(
    url: str,
    headers: dict[str, str],
    *,
    model: str | None = None,
) -> str:
    """Scope cooldowns to one upstream endpoint, credential, and model."""

    credential = ""
    for name, value in headers.items():
        if name.lower() in {"authorization", "x-api-key"}:
            credential = value
            break
    model_scope = (model or "").strip().lower()
    return hashlib.sha256(f"{url}\0{credential}\0{model_scope}".encode()).hexdigest()[:20]


def cooldown_delay_seconds(
    response: httpx.Response | Any,
    policy: UpstreamCooldownPolicy,
    remaining_budget_seconds: float,
) -> float:
    """Resolve Retry-After without the ordinary short-retry 30 second cap."""

    cap_ms = max(1, int(min(policy.max_wait_seconds, remaining_budget_seconds) * 1000))
    delay_ms = retry_after_ms(response, cap_ms)
    if delay_ms is None:
        return min(policy.default_retry_seconds, remaining_budget_seconds)
    return min(max(delay_ms / 1000.0, 0.05), remaining_budget_seconds)


def should_hold_status(status_code: int, policy: UpstreamCooldownPolicy) -> bool:
    """Return whether an HTTP status is safe to replay before response bytes."""

    return policy.enabled and status_code in policy.hold_statuses


def _anthropic_error_payload(status_code: int, body: bytes, fallback: str) -> dict[str, Any]:
    error_type = "rate_limit_error" if status_code == 429 else "api_error"
    message = fallback
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        source = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
        if isinstance(source, dict):
            error_type = str(source.get("type") or error_type)
            message = str(source.get("message") or message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {"type": "error", "error": {"type": error_type, "message": message}}


def _anthropic_error_event(status_code: int, body: bytes, fallback: str) -> bytes:
    payload = _anthropic_error_payload(status_code, body, fallback)
    return f"event: error\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _anthropic_error_json(status_code: int, body: bytes, fallback: str) -> bytes:
    payload = _anthropic_error_payload(status_code, body, fallback)
    return json.dumps(payload, separators=(",", ":")).encode()


class CooldownHeldStream:
    """Response-like object that resumes one upstream stream after its cooldown."""

    status_code = 200

    def __init__(
        self,
        *,
        owner: Any,
        http_client: httpx.AsyncClient,
        url: str,
        outbound_bytes: bytes,
        outbound_headers: dict[str, str],
        request_id: str,
        policy: UpstreamCooldownPolicy,
        model: str | None = None,
        initial_delay_seconds: float | None = None,
        initial_status_code: int = 429,
        json_mode: bool = False,
    ) -> None:
        self._json_mode = json_mode
        content_type = "application/json" if json_mode else "text/event-stream"
        self.headers = httpx.Headers(
            {
                "content-type": content_type,
                "x-headroom-upstream-cooldown-hold": "1",
                "x-headroom-upstream-recovery-hold": "1",
            }
        )
        self._gate = get_upstream_cooldown_gate(owner)
        self._http_client = http_client
        self._url = url
        self._outbound_bytes = outbound_bytes
        self._outbound_headers = outbound_headers
        self._request_id = request_id
        self._policy = policy
        self._route_key = upstream_route_key(url, outbound_headers, model=model)
        self._initial_delay_seconds = initial_delay_seconds
        self._initial_status_code = initial_status_code
        self._active_response: httpx.Response | Any | None = None
        self._pending_task: asyncio.Task[Any] | None = None
        self._closed = False

    def _keepalive(self) -> bytes:
        return _JSON_KEEPALIVE if self._json_mode else _PING_EVENT

    def _error(self, status_code: int, body: bytes, fallback: str) -> bytes:
        if self._json_mode:
            return _anthropic_error_json(status_code, body, fallback)
        return _anthropic_error_event(status_code, body, fallback)

    async def aclose(self) -> None:
        self._closed = True
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending_task
        self._pending_task = None
        if self._active_response is not None:
            await self._active_response.aclose()
            self._active_response = None

    async def _wait_with_pings(self, delay_seconds: float) -> AsyncIterator[bytes]:
        wake_at = time.monotonic() + max(0.0, delay_seconds)
        while not self._closed:
            remaining = wake_at - time.monotonic()
            if remaining <= 0:
                return
            yield self._keepalive()
            await asyncio.sleep(min(self._policy.heartbeat_seconds, remaining))

    async def _wait_task_with_pings(
        self, task: asyncio.Task[Any], deadline: float
    ) -> AsyncIterator[bytes]:
        while not task.done() and not self._closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return
            done, _ = await asyncio.wait(
                {task}, timeout=min(self._policy.heartbeat_seconds, remaining)
            )
            if not done:
                yield self._keepalive()

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        deadline = time.monotonic() + self._policy.max_wait_seconds
        last_error_body = b""
        last_error_status = self._initial_status_code
        outcome = "cancelled"
        if self._initial_delay_seconds is not None:
            self._gate.defer(self._route_key, self._initial_delay_seconds)

        self._gate.hold_started()

        logger.warning(
            "[%s] upstream_cooldown_hold_start route=%s max_wait_s=%.0f",
            self._request_id,
            self._route_key,
            self._policy.max_wait_seconds,
        )

        try:
            while not self._closed:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break

                remaining = min(self._gate.remaining(self._route_key), budget)
                if remaining > 0:
                    async for ping in self._wait_with_pings(remaining):
                        yield ping
                    continue

                lock = self._gate.probe_lock(self._route_key)
                acquire_task = asyncio.create_task(lock.acquire())
                self._pending_task = acquire_task
                acquired = False
                try:
                    async for ping in self._wait_task_with_pings(acquire_task, deadline):
                        yield ping
                    self._pending_task = None
                    if not acquire_task.done() or acquire_task.cancelled():
                        break
                    acquired = bool(acquire_task.result())

                    # Another waiter may have extended the cooldown while this
                    # request was queued for the single probe slot.
                    remaining = min(
                        self._gate.remaining(self._route_key),
                        max(0.0, deadline - time.monotonic()),
                    )
                    if remaining > 0:
                        continue

                    request = self._http_client.build_request(
                        "POST",
                        self._url,
                        content=self._outbound_bytes,
                        headers=self._outbound_headers,
                    )
                    send_task = asyncio.create_task(self._http_client.send(request, stream=True))
                    self._pending_task = send_task
                    async for ping in self._wait_task_with_pings(send_task, deadline):
                        yield ping
                    self._pending_task = None
                    if not send_task.done() or send_task.cancelled():
                        break

                    try:
                        response = send_task.result()
                    except httpx.TransportError as error:
                        self._gate.record_transport_failure()
                        delay = min(
                            self._policy.default_retry_seconds,
                            max(0.0, deadline - time.monotonic()),
                        )
                        self._gate.defer(self._route_key, delay)
                        logger.warning(
                            "[%s] upstream_cooldown_probe_transport_error route=%s "
                            "retry_s=%.2f error=%r",
                            self._request_id,
                            self._route_key,
                            delay,
                            error,
                        )
                        continue

                    if should_hold_status(response.status_code, self._policy):
                        last_error_status = response.status_code
                        try:
                            last_error_body = await response.aread()
                        except Exception:
                            last_error_body = b""
                        budget = max(0.0, deadline - time.monotonic())
                        delay = cooldown_delay_seconds(response, self._policy, budget)
                        await response.aclose()
                        self._gate.defer(self._route_key, delay)
                        logger.warning(
                            "[%s] upstream_cooldown_probe_deferred route=%s status=%s retry_s=%.2f",
                            self._request_id,
                            self._route_key,
                            response.status_code,
                            delay,
                        )
                        continue

                    self._gate.clear(self._route_key)
                    if response.status_code >= 400:
                        try:
                            body = await response.aread()
                        except Exception:
                            body = b""
                        await response.aclose()
                        yield self._error(
                            response.status_code,
                            body,
                            f"Upstream returned HTTP {response.status_code} after cooldown",
                        )
                        return

                    self._active_response = response
                finally:
                    # Cancellation can race with lock.acquire() between the last
                    # heartbeat and the first line after the await. Recover the
                    # acquire result here so a disconnected client cannot wedge
                    # every later request behind a leaked probe slot.
                    if not acquired and acquire_task.done() and not acquire_task.cancelled():
                        with contextlib.suppress(Exception):
                            acquired = bool(acquire_task.result())
                    if acquired and lock.locked():
                        lock.release()

                logger.info(
                    "[%s] upstream_cooldown_hold_recovered route=%s",
                    self._request_id,
                    self._route_key,
                )
                outcome = "recovered"
                async for chunk in self._active_response.aiter_bytes():
                    yield chunk
                return

            outcome = "timeout"
            yield self._error(
                last_error_status,
                last_error_body,
                "Upstream service did not recover before the configured hold deadline",
            )
        finally:
            self._gate.hold_finished(outcome)
            await self.aclose()
