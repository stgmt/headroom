"""Unit 2: stage-timing instrumentation on the Anthropic HTTP path."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import httpx
import pytest
from fastapi import Request

from headroom.proxy.handlers.anthropic import AnthropicHandlerMixin
from headroom.proxy.models import ProxyConfig


class _DummyTokenizer:
    def count_messages(self, messages) -> int:
        return 1


class _DummyMetrics:
    def __init__(self) -> None:
        self.stage_timings: list[tuple[str, dict[str, float]]] = []

    async def record_request(self, **kwargs):
        return None

    async def record_stage_timings(self, path: str, timings: dict[str, float]) -> None:
        self.stage_timings.append((path, dict(timings)))

    async def record_failed(self, **kwargs):
        return None

    async def record_rate_limited(self, **kwargs):
        return None


class _ResponseStub:
    status_code = 200
    headers: dict[str, str] = {}
    content = b'{"id":"msg_1","type":"message","role":"assistant","content":[],"usage":{"input_tokens":1,"output_tokens":1}}'

    def json(self):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _DummyAnthropicHandler(AnthropicHandlerMixin):
    ANTHROPIC_API_URL = "https://api.anthropic.com"

    def __init__(self) -> None:
        self.rate_limiter = None
        self.metrics = _DummyMetrics()
        self.config = ProxyConfig(
            optimize=False,
            image_optimize=False,
            retry_max_attempts=1,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            connect_timeout_seconds=10,
            mode="token",
            cache_enabled=False,
            rate_limit_enabled=False,
            fallback_enabled=False,
            fallback_provider=None,
            prefix_freeze_enabled=False,
            memory_enabled=False,
        )
        self.usage_reporter = None
        self.anthropic_provider = SimpleNamespace(get_context_limit=lambda model: 200_000)
        self.anthropic_pipeline = SimpleNamespace(apply=MagicMock())
        self.anthropic_backend = None
        self.cost_tracker = None
        self.memory_handler = None
        self.cache = None
        self.security = None
        self.ccr_context_tracker = None
        self.ccr_injector = None
        self.ccr_response_handler = None
        self.ccr_feedback = None
        self.ccr_batch_processor = None
        self.ccr_mcp_server = None
        self.traffic_learner = None
        self.tool_injector = None
        self.read_lifecycle_manager = None
        self.logger = SimpleNamespace(log=lambda *a, **k: None)
        self.request_logger = self.logger
        self.usage_observer = None
        self.image_compressor = None
        self.session_tracker_store = SimpleNamespace(
            compute_session_id=lambda *a, **k: "sess-1",
            get_or_create=lambda *a, **k: SimpleNamespace(
                get_frozen_message_count=lambda: 0,
                get_last_original_messages=lambda: [],
                get_last_forwarded_messages=lambda: [],
                record_request=lambda *a, **k: None,
            ),
        )

    async def _next_request_id(self) -> str:
        return "req-anth-test"

    def _extract_tags(self, headers):
        return {}

    async def _retry_request(
        self,
        method: str,
        url: str,
        headers: dict,
        body: dict,
        **_kwargs,
    ):
        self.captured = (method, url, headers, body)
        return _ResponseStub()

    def _get_compression_cache(self, session_id):
        return SimpleNamespace(
            apply_cached=lambda m: m,
            compute_frozen_count=lambda m: 0,
            mark_stable_from_messages=lambda *a, **k: None,
            should_defer_compression=lambda h: False,
            mark_stable=lambda h: None,
            content_hash=lambda c: "h",
            update_from_result=lambda *a, **k: None,
            _cache={},
            _stable_hashes=set(),
        )


def _build_request(body: dict, headers: dict[str, str]) -> Request:
    payload = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [
            (key.lower().encode("utf-8"), value.encode("utf-8")) for key, value in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    return Request(scope, receive)


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def stage_log_capture():
    target = logging.getLogger("headroom.proxy")
    handler = _CapturingHandler()
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)


def _parse_stage_log(handler: _CapturingHandler) -> dict:
    for record in handler.records:
        msg = record.getMessage()
        if "STAGE_TIMINGS" in msg:
            payload_start = msg.index("STAGE_TIMINGS ") + len("STAGE_TIMINGS ")
            return json.loads(msg[payload_start:])
    raise AssertionError("no STAGE_TIMINGS log line captured")


def test_anthropic_http_happy_path_emits_stage_timings(stage_log_capture):
    request = _build_request(
        {
            "model": "claude-3-5-sonnet-latest",
            "messages": [{"role": "user", "content": "hello"}],
        },
        {"authorization": "Bearer sk-ant-api-test"},
    )
    handler = _DummyAnthropicHandler()

    # Force tokenizer to a stub.
    import headroom.tokenizers as _tk

    orig_get = _tk.get_tokenizer
    _tk.get_tokenizer = lambda model: _DummyTokenizer()
    try:
        anyio.run(handler.handle_anthropic_messages, request)
    finally:
        _tk.get_tokenizer = orig_get

    payload = _parse_stage_log(stage_log_capture)
    assert payload["event"] == "stage_timings"
    assert payload["path"] == "anthropic_messages"
    assert payload["request_id"] == "req-anth-test"
    assert payload["session_id"]
    stages = payload["stages"]

    for key in (
        "read_request_json",
        "deep_copy",
        "compression_first_stage",
        "memory_context",
        "upstream_connect",
        "upstream_first_byte",
        "total_pre_upstream",
    ):
        assert key in stages, f"missing stage: {key}"

    assert stages["read_request_json"] is not None
    assert stages["deep_copy"] is not None
    assert stages["upstream_connect"] is not None
    assert stages["upstream_first_byte"] is not None
    assert stages["total_pre_upstream"] is not None
    # compression + memory_context were skipped (optimize=False, no memory)
    assert stages["compression_first_stage"] is None
    assert stages["memory_context"] is None

    # Metrics sink got the observation too.
    assert handler.metrics.stage_timings
    path, emitted = handler.metrics.stage_timings[-1]
    assert path == "anthropic_messages"
    assert "total_pre_upstream" in emitted


@pytest.mark.parametrize("failure_kind", ["429", "503", "transport"])
def test_anthropic_buffered_failures_enter_json_recovery_hold(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HOLD_ENABLED", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_MAX_WAIT_SECONDS", "1")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setenv("HEADROOM_UPSTREAM_429_DEFAULT_RETRY_SECONDS", "0.01")

    async def scenario() -> None:
        request = _build_request(
            {
                "model": "claude-opus-5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
            {"authorization": "Bearer stable-group-key"},
        )
        handler = _DummyAnthropicHandler()
        upstream_request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        initial = httpx.Response(
            429,
            request=upstream_request,
            headers={"retry-after": "0.01", "content-type": "application/json"},
            json={
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "provider reset"},
            },
        )

        async def initial_retry(*args, **kwargs):
            if failure_kind == "429":
                return initial
            if failure_kind == "503":
                response = httpx.Response(
                    503,
                    request=upstream_request,
                    headers={"retry-after": "0.01"},
                    json={"type": "error", "error": {"message": "overloaded"}},
                )
                raise httpx.HTTPStatusError(
                    "overloaded",
                    request=upstream_request,
                    response=response,
                )
            raise httpx.ConnectError("stack restarting", request=upstream_request)

        calls = 0

        async def recovered(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "msg_recovered",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "BUFFERED_RECOVERY_OK"}],
                    "model": "claude-opus-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        handler._retry_request = initial_retry  # type: ignore[method-assign]
        handler.http_client = httpx.AsyncClient(transport=httpx.MockTransport(recovered))

        import headroom.tokenizers as _tk

        orig_get = _tk.get_tokenizer
        _tk.get_tokenizer = lambda model: _DummyTokenizer()
        try:
            response = await handler.handle_anthropic_messages(request)
            wire = b"".join([chunk async for chunk in response.body_iterator])
        finally:
            _tk.get_tokenizer = orig_get
            await handler.http_client.aclose()

        payload = json.loads(wire)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert wire.startswith(b" \n")
        assert calls == 1
        assert payload["content"][0]["text"] == "BUFFERED_RECOVERY_OK"

    anyio.run(scenario)


def test_anthropic_no_optimize_preserves_client_tool_order():
    tools = [
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "Bash",
            "description": "Run a shell command",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]
    request = _build_request(
        {
            "model": "claude-3-5-sonnet-latest",
            "messages": [{"role": "user", "content": "use a tool"}],
            "tools": tools,
        },
        {"authorization": "Bearer sk-ant-api-test"},
    )
    handler = _DummyAnthropicHandler()

    import headroom.tokenizers as _tk

    orig_get = _tk.get_tokenizer
    _tk.get_tokenizer = lambda model: _DummyTokenizer()
    try:
        anyio.run(handler.handle_anthropic_messages, request)
    finally:
        _tk.get_tokenizer = orig_get

    _, _, _, forwarded_body = handler.captured
    assert [tool["name"] for tool in forwarded_body["tools"]] == ["Read", "Bash"]


def test_anthropic_http_invalid_body_still_emits_stage_timings(stage_log_capture):
    async def receive():
        # Invalid JSON — produces ``ValueError`` from ``_read_request_json``.
        return {"type": "http.request", "body": b"not-json", "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer sk-ant-api-test")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    request = Request(scope, receive)
    handler = _DummyAnthropicHandler()

    anyio.run(handler.handle_anthropic_messages, request)

    payload = _parse_stage_log(stage_log_capture)
    stages = payload["stages"]
    # Even on invalid body, ``read_request_json`` duration is recorded
    # (the measure wraps the call, including the raised exception path).
    assert stages["read_request_json"] is not None
    # Downstream stages never ran:
    assert stages["upstream_connect"] is None
    assert stages["upstream_first_byte"] is None


def test_anthropic_http_request_and_session_ids_present(stage_log_capture):
    request = _build_request(
        {
            "model": "claude-3-5-sonnet-latest",
            "messages": [{"role": "user", "content": "hi"}],
        },
        {"authorization": "Bearer sk-ant-api-test"},
    )
    handler = _DummyAnthropicHandler()

    import headroom.tokenizers as _tk

    orig_get = _tk.get_tokenizer
    _tk.get_tokenizer = lambda model: _DummyTokenizer()
    try:
        anyio.run(handler.handle_anthropic_messages, request)
    finally:
        _tk.get_tokenizer = orig_get

    payload = _parse_stage_log(stage_log_capture)
    assert payload["request_id"] == "req-anth-test"
    assert isinstance(payload["session_id"], str)
    assert len(payload["session_id"]) >= 16
