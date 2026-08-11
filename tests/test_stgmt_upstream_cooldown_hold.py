"""Regression tests for long-lived Claude Code upstream recovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from headroom.proxy.upstream_cooldown import (
    CooldownHeldStream,
    UpstreamCooldownPolicy,
    cooldown_delay_seconds,
    get_upstream_cooldown_gate,
    should_hold_status,
    upstream_route_key,
)


class _Owner:
    pass


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _response(status: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"content-type": "text/event-stream"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    if status >= 400:
        body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "rate_limit_error" if status == 429 else "api_error",
                    "message": f"upstream {status}",
                },
            }
        ).encode()
        return httpx.Response(status, headers=headers, content=body)
    chunks = [
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_ok"}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    return httpx.Response(status, headers=headers, stream=_BytesStream(chunks))


def _json_response(status: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    if status >= 400:
        payload = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": f"upstream {status}"},
        }
    else:
        payload = {
            "id": "msg_json_ok",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "JSON_RECOVERY_OK"}],
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    return httpx.Response(status, headers=headers, content=json.dumps(payload).encode())


@dataclass
class _FakeClient:
    responses: list[httpx.Response | httpx.TransportError]
    header_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        self.calls = 0
        self.active_sends = 0
        self.max_active_sends = 0

    def build_request(self, method: str, url: str, **kwargs) -> httpx.Request:
        return httpx.Request(method, url, **kwargs)

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        self.calls += 1
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        try:
            if self.header_delay_seconds:
                await asyncio.sleep(self.header_delay_seconds)
            response = self.responses.pop(0)
            if isinstance(response, httpx.TransportError):
                raise response
            response.request = request
            return response
        finally:
            self.active_sends -= 1


def _policy(*, max_wait: float = 0.5) -> UpstreamCooldownPolicy:
    return UpstreamCooldownPolicy(
        enabled=True,
        max_wait_seconds=max_wait,
        heartbeat_seconds=0.01,
        default_retry_seconds=0.01,
    )


def test_retry_after_is_not_capped_by_ordinary_30_second_backoff() -> None:
    response = _response(429, retry_after="2700")
    policy = UpstreamCooldownPolicy(
        enabled=True,
        max_wait_seconds=21600,
        heartbeat_seconds=15,
        default_retry_seconds=30,
    )

    assert cooldown_delay_seconds(response, policy, 21600) == 2700


def test_cooldown_route_is_scoped_by_model() -> None:
    async def scenario() -> None:
        owner = _Owner()
        gate = get_upstream_cooldown_gate(owner)
        url = "http://sub2api:8080/v1/messages"
        headers = {"authorization": "Bearer stable-group-key"}
        sonnet_key = upstream_route_key(url, headers, model="claude-sonnet-5")
        sol_key = upstream_route_key(url, headers, model="gpt-5.6-sol")

        gate.defer(sonnet_key, 60)

        assert sonnet_key != sol_key
        assert gate.remaining(sonnet_key) > 0
        assert gate.remaining(sol_key) == 0

    asyncio.run(scenario())


def test_recovery_policy_holds_only_configured_transient_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HOLD_ENABLED", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_RECOVERY_HOLD_STATUSES", "429,503,529")
    policy = UpstreamCooldownPolicy.from_env()

    assert should_hold_status(429, policy)
    assert should_hold_status(503, policy)
    assert should_hold_status(529, policy)
    assert not should_hold_status(400, policy)
    assert not should_hold_status(401, policy)
    assert not should_hold_status(502, policy)


def _held(
    owner: _Owner,
    client: _FakeClient,
    *,
    request_id: str,
    initial_delay: float | None = 0.01,
    max_wait: float = 0.5,
) -> CooldownHeldStream:
    return CooldownHeldStream(
        owner=owner,
        http_client=client,  # type: ignore[arg-type]
        url="http://sub2api:8080/v1/messages",
        outbound_bytes=b'{"stream":true}',
        outbound_headers={"authorization": "Bearer stable-group-key"},
        request_id=request_id,
        policy=_policy(max_wait=max_wait),
        initial_delay_seconds=initial_delay,
    )


async def _consume(stream: CooldownHeldStream) -> bytes:
    chunks: list[bytes] = []
    async for chunk in stream.aiter_bytes():
        chunks.append(chunk)
    return b"".join(chunks)


def test_long_retry_after_stays_sse_and_recovers_without_client_429() -> None:
    async def scenario() -> None:
        owner = _Owner()
        client = _FakeClient([_response(429, retry_after="0.02"), _response(200)])

        wire = await _consume(_held(owner, client, request_id="recover"))

        assert client.calls == 2
        assert b"event: ping" in wire
        assert b"event: message_start" in wire
        assert b"event: message_stop" in wire
        assert b"event: error" not in wire

    asyncio.run(scenario())


def test_buffered_json_hold_uses_whitespace_keepalive_then_valid_json() -> None:
    async def scenario() -> None:
        owner = _Owner()
        client = _FakeClient([_json_response(429, retry_after="0.02"), _json_response(200)])
        held = CooldownHeldStream(
            owner=owner,
            http_client=client,  # type: ignore[arg-type]
            url="http://sub2api:8080/v1/messages",
            outbound_bytes=b'{"stream":false}',
            outbound_headers={"authorization": "Bearer stable-group-key"},
            request_id="json-recover",
            policy=_policy(),
            initial_delay_seconds=0.01,
            json_mode=True,
        )

        wire = await _consume(held)
        payload = json.loads(wire)

        assert client.calls == 2
        assert wire.startswith(b" \n")
        assert b"event: ping" not in wire
        assert payload["content"][0]["text"] == "JSON_RECOVERY_OK"

    asyncio.run(scenario())


def test_recovery_metrics_track_holds_recovery_and_transport_failures() -> None:
    async def scenario() -> None:
        owner = _Owner()
        request = httpx.Request("POST", "http://sub2api:8080/v1/messages")
        client = _FakeClient(
            [httpx.ConnectError("temporary disconnect", request=request), _response(200)]
        )

        wire = await _consume(_held(owner, client, request_id="metrics", initial_delay=None))
        snapshot = get_upstream_cooldown_gate(owner).snapshot()

        assert b"event: message_stop" in wire
        assert snapshot["active_holds"] == 0
        assert snapshot["holds_total"] == 1
        assert snapshot["recoveries_total"] == 1
        assert snapshot["transport_failures_total"] == 1
        assert snapshot["timeouts_total"] == 0

    asyncio.run(scenario())


def test_concurrent_waiters_serialize_upstream_header_probes() -> None:
    async def scenario() -> None:
        owner = _Owner()
        count = 10
        client = _FakeClient(
            [_response(200) for _ in range(count)],
            header_delay_seconds=0.02,
        )

        results = await asyncio.gather(
            *[
                _consume(_held(owner, client, request_id=f"waiter-{index}"))
                for index in range(count)
            ]
        )

        assert client.calls == count
        assert client.max_active_sends == 1
        assert all(b"event: message_stop" in wire for wire in results)

    asyncio.run(scenario())


def test_hold_deadline_returns_valid_anthropic_sse_error() -> None:
    async def scenario() -> None:
        owner = _Owner()
        client = _FakeClient([_response(429, retry_after="0.01") for _ in range(20)])

        wire = await _consume(_held(owner, client, request_id="deadline", max_wait=0.06))

        assert client.calls >= 1
        assert b"event: ping" in wire
        assert b"event: error" in wire
        assert b'"type":"rate_limit_error"' in wire

    asyncio.run(scenario())


def test_non_retryable_error_is_not_hidden_by_cooldown_hold() -> None:
    async def scenario() -> None:
        owner = _Owner()
        client = _FakeClient([_response(401)])

        wire = await _consume(_held(owner, client, request_id="auth-error"))

        assert client.calls == 1
        assert b"event: error" in wire
        assert b"upstream 401" in wire

    asyncio.run(scenario())


def test_client_cancellation_releases_probe_slot_and_cancels_send() -> None:
    async def scenario() -> None:
        owner = _Owner()
        client = _FakeClient([_response(200)], header_delay_seconds=1.0)
        first = _held(
            owner,
            client,
            request_id="cancelled",
            initial_delay=None,
        )

        iterator = first.aiter_bytes()
        assert await anext(iterator) == b'event: ping\ndata: {"type":"ping"}\n\n'
        await iterator.aclose()

        client.header_delay_seconds = 0.0
        wire = await _consume(_held(owner, client, request_id="after-cancel", initial_delay=None))
        assert b"event: message_stop" in wire
        assert client.active_sends == 0

    asyncio.run(scenario())


def test_anthropic_streaming_handler_turns_initial_429_into_held_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from headroom.proxy.server import HeadroomProxy

    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HOLD_ENABLED", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_MAX_WAIT_SECONDS", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_DEFAULT_RETRY_SECONDS", "0.01")

    async def scenario() -> None:
        proxy = object.__new__(HeadroomProxy)
        client = _FakeClient([_response(429, retry_after="0.02"), _response(200)])
        proxy.http_client = client
        proxy.config = MagicMock(
            retry_enabled=True,
            retry_max_attempts=1,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            ccr_inject_tool=False,
        )
        proxy.memory_handler = None
        proxy._parse_sse_usage_from_buffer = MagicMock(return_value=None)
        proxy._finalize_stream_response = AsyncMock(return_value=None)

        response = await proxy._stream_response(
            url="http://sub2api:8080/v1/messages",
            headers={"authorization": "Bearer stable-group-key"},
            body={
                "model": "claude-sonnet-5",
                "max_tokens": 32,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            provider="anthropic",
            model="claude-sonnet-5",
            request_id="handler-hold",
            original_tokens=5,
            optimized_tokens=5,
            tokens_saved=0,
            transforms_applied=[],
            tags={},
            optimization_latency=0.0,
        )

        wire = b"".join([chunk async for chunk in response.body_iterator])
        assert response.status_code == 200
        assert response.media_type == "text/event-stream"
        assert client.calls == 2
        assert b"event: ping" in wire
        assert b"event: message_stop" in wire
        assert b"event: error" not in wire
        proxy._finalize_stream_response.assert_awaited_once()

    asyncio.run(scenario())


def test_anthropic_streaming_handler_holds_503_after_short_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from headroom.proxy.server import HeadroomProxy

    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HOLD_ENABLED", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_MAX_WAIT_SECONDS", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_DEFAULT_RETRY_SECONDS", "0.01")

    async def scenario() -> None:
        proxy = object.__new__(HeadroomProxy)
        client = _FakeClient([_response(503), _response(503), _response(200)])
        proxy.http_client = client
        proxy.config = MagicMock(
            retry_enabled=True,
            retry_max_attempts=2,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            ccr_inject_tool=False,
        )
        proxy.memory_handler = None
        proxy._parse_sse_usage_from_buffer = MagicMock(return_value=None)
        proxy._finalize_stream_response = AsyncMock(return_value=None)

        response = await proxy._stream_response(
            url="http://sub2api:8080/v1/messages",
            headers={"authorization": "Bearer stable-group-key"},
            body={
                "model": "claude-sonnet-5",
                "max_tokens": 32,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            provider="anthropic",
            model="claude-sonnet-5",
            request_id="handler-503-hold",
            original_tokens=5,
            optimized_tokens=5,
            tokens_saved=0,
            transforms_applied=[],
            tags={},
            optimization_latency=0.0,
        )

        wire = b"".join([chunk async for chunk in response.body_iterator])
        assert response.status_code == 200
        assert client.calls == 3
        assert b"event: ping" in wire
        assert b"event: message_stop" in wire
        assert b"event: error" not in wire

    asyncio.run(scenario())


def test_anthropic_streaming_handler_holds_after_transport_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from headroom.proxy.server import HeadroomProxy

    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HOLD_ENABLED", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_MAX_WAIT_SECONDS", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_DEFAULT_RETRY_SECONDS", "0.01")

    async def scenario() -> None:
        proxy = object.__new__(HeadroomProxy)
        request = httpx.Request("POST", "http://sub2api:8080/v1/messages")
        client = _FakeClient(
            [httpx.ConnectError("stack restarting", request=request), _response(200)]
        )
        proxy.http_client = client
        proxy.config = MagicMock(
            retry_enabled=True,
            retry_max_attempts=1,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            ccr_inject_tool=False,
        )
        proxy.memory_handler = None
        proxy._parse_sse_usage_from_buffer = MagicMock(return_value=None)
        proxy._finalize_stream_response = AsyncMock(return_value=None)

        response = await proxy._stream_response(
            url="http://sub2api:8080/v1/messages",
            headers={"authorization": "Bearer stable-group-key"},
            body={
                "model": "claude-sonnet-5",
                "max_tokens": 32,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            provider="anthropic",
            model="claude-sonnet-5",
            request_id="handler-transport-hold",
            original_tokens=5,
            optimized_tokens=5,
            tokens_saved=0,
            transforms_applied=[],
            tags={},
            optimization_latency=0.0,
        )

        wire = b"".join([chunk async for chunk in response.body_iterator])
        assert response.status_code == 200
        assert client.calls == 2
        assert b"event: ping" in wire
        assert b"event: message_stop" in wire
        assert b"event: error" not in wire

    asyncio.run(scenario())


def test_disabling_hold_reproduces_client_visible_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from headroom.proxy.server import HeadroomProxy

    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HOLD_ENABLED", "0")

    async def scenario() -> None:
        proxy = object.__new__(HeadroomProxy)
        upstream = _response(429, retry_after="2700")
        client = _FakeClient([upstream])
        proxy.http_client = client
        proxy.config = MagicMock(
            retry_enabled=True,
            retry_max_attempts=1,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            ccr_inject_tool=False,
        )
        proxy.memory_handler = None
        proxy.metrics = MagicMock()
        proxy.cost_tracker = MagicMock()
        proxy.cost_tracker.estimate_cost.return_value = 0.0
        proxy.stats = {
            "requests_total": 0,
            "requests_optimized": 0,
            "tokens": {"original": 0, "optimized": 0, "saved": 0},
            "cost": {"total_usd": 0.0, "savings_usd": 0.0},
            "errors": 0,
            "active_requests": 0,
            "requests_per_model": {},
        }
        proxy._record_request_outcome = AsyncMock(return_value=None)

        response = await proxy._stream_response(
            url="http://sub2api:8080/v1/messages",
            headers={"authorization": "Bearer stable-group-key"},
            body={
                "model": "claude-sonnet-5",
                "max_tokens": 32,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            provider="anthropic",
            model="claude-sonnet-5",
            request_id="handler-no-hold",
            original_tokens=5,
            optimized_tokens=5,
            tokens_saved=0,
            transforms_applied=[],
            tags={},
            optimization_latency=0.0,
        )

        assert response.status_code == 429
        assert client.calls == 1
        assert b"upstream 429" in response.body

    asyncio.run(scenario())


def test_transient_hold_budget_is_capped_but_429_keeps_full_retry_after_budget() -> None:
    owner = _Owner()
    client = _FakeClient([])
    policy = UpstreamCooldownPolicy(
        enabled=True,
        max_wait_seconds=21600,
        heartbeat_seconds=1,
        default_retry_seconds=30,
        transient_max_wait_seconds=90,
    )

    transient = CooldownHeldStream(
        owner=owner,
        http_client=client,  # type: ignore[arg-type]
        url="http://sub2api:8080/v1/messages",
        outbound_bytes=b'{}',
        outbound_headers={"authorization": "Bearer stable-group-key"},
        request_id="transient-cap",
        policy=policy,
        initial_status_code=502,
    )
    rate_limited = CooldownHeldStream(
        owner=owner,
        http_client=client,  # type: ignore[arg-type]
        url="http://sub2api:8080/v1/messages",
        outbound_bytes=b'{}',
        outbound_headers={"authorization": "Bearer stable-group-key"},
        request_id="rate-limit-budget",
        policy=policy,
        initial_status_code=429,
    )

    assert transient._policy.max_wait_seconds == 90
    assert rate_limited._policy.max_wait_seconds == 21600
