from __future__ import annotations

import json

from headroom.proxy.claude_stream_recovery import (
    AnthropicCheckpointRelay,
    ClaudeStreamRecoveryStore,
)


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _message_start() -> bytes:
    return _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "gpt-5.6-luna",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
    )


def _text_start(index: int = 0) -> bytes:
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    )


def _text_delta(text: str, index: int = 0) -> bytes:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _tool_start(index: int = 0) -> bytes:
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_partial",
                "name": "Bash",
                "input": {},
            },
        },
    )


def _tool_delta(partial_json: str, index: int = 0) -> bytes:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        },
    )


def _block_stop(index: int = 0) -> bytes:
    return _sse(
        "content_block_stop",
        {"type": "content_block_stop", "index": index},
    )


def _message_delta(stop_reason: str) -> bytes:
    return _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 12},
        },
    )


def _message_stop() -> bytes:
    return _sse("message_stop", {"type": "message_stop"})


def _upstream_error() -> bytes:
    return _sse(
        "error",
        {
            "type": "error",
            "error": {"type": "api_error", "message": "peer reset mid-response"},
        },
    )


def _relay(tmp_path, *, max_attempts: int = 3):
    store = ClaudeStreamRecoveryStore(
        tmp_path / "recovery.sqlite3",
        ttl_seconds=300,
        max_attempts=max_attempts,
    )
    relay = AnthropicCheckpointRelay(
        store=store,
        session_key="claude-code:session-1:main",
        request_id="request-1",
    )
    return relay, store


def test_partial_text_is_closed_and_scheduled_for_same_session_continuation(tmp_path):
    relay, store = _relay(tmp_path)

    prefix = _message_start() + _text_start() + _text_delta("Work completed so far.")
    assert b"Work completed so far." in b"".join(relay.feed(prefix))

    terminal = b"".join(relay.feed(_upstream_error()))

    assert b"event: error" not in terminal
    assert b'"type": "content_block_stop"' in terminal
    assert b'"stop_reason": "end_turn"' in terminal
    assert terminal.endswith(_message_stop())
    assert relay.checkpointed is True

    marker = store.consume("claude-code:session-1:main")
    assert marker["pending"] is True
    assert marker["request_id"] == "request-1"
    assert marker["failure_count"] == 1
    assert store.consume("claude-code:session-1:main")["pending"] is False


def test_partial_tool_json_is_never_released_to_claude(tmp_path):
    relay, store = _relay(tmp_path)

    assert b"".join(relay.feed(_message_start())) == _message_start()
    held = relay.feed(_tool_start() + _tool_delta('{"command":"git sta'))
    assert held == []

    terminal = b"".join(relay.feed(_upstream_error()))

    assert b"toolu_partial" not in terminal
    assert b"git sta" not in terminal
    assert b'"stop_reason": "end_turn"' in terminal
    assert terminal.endswith(_message_stop())
    assert store.consume("claude-code:session-1:main")["pending"] is True


def test_complete_tool_block_is_released_atomically_and_does_not_need_stop_hook(tmp_path):
    relay, store = _relay(tmp_path)
    relay.feed(_message_start())

    assert relay.feed(_tool_start()) == []
    assert relay.feed(_tool_delta('{"command":"git status"}')) == []
    released = b"".join(relay.feed(_block_stop()))
    assert b"toolu_partial" in released
    assert b"git status" in released
    assert released.endswith(_block_stop())

    tail = b"".join(relay.feed(_message_delta("tool_use") + _message_stop()))
    assert tail.endswith(_message_stop())
    assert relay.message_stopped is True
    assert relay.checkpointed is False
    assert store.consume("claude-code:session-1:main")["pending"] is False


def test_clean_eof_without_message_stop_becomes_checkpoint(tmp_path):
    relay, store = _relay(tmp_path)
    relay.feed(_message_start() + _text_start() + _text_delta("partial"))

    terminal = b"".join(relay.finish("missing_terminal_event"))

    assert terminal.endswith(_message_stop())
    assert b"event: error" not in terminal
    marker = store.consume("claude-code:session-1:main")
    assert marker["pending"] is True
    assert marker["reason"] == "missing_terminal_event"


def test_error_before_message_start_is_not_hidden(tmp_path):
    relay, store = _relay(tmp_path)

    output = b"".join(relay.feed(_upstream_error()))

    assert b"event: error" in output
    assert relay.checkpointed is False
    assert store.consume("claude-code:session-1:main")["pending"] is False


def test_normal_stop_clears_prior_recovery_chain(tmp_path):
    relay, store = _relay(tmp_path)
    store.mark("claude-code:session-1:main", request_id="old", reason="transport")

    output = b"".join(
        relay.feed(
            _message_start()
            + _text_start()
            + _text_delta("done")
            + _block_stop()
            + _message_delta("end_turn")
            + _message_stop()
        )
    )

    assert output.endswith(_message_stop())
    assert store.consume("claude-code:session-1:main")["pending"] is False
    assert store.stats()["tracked_sessions"] == 0


def test_recovery_chain_stops_after_configured_attempt_limit(tmp_path):
    store = ClaudeStreamRecoveryStore(
        tmp_path / "recovery.sqlite3",
        ttl_seconds=300,
        max_attempts=2,
    )
    key = "claude-code:session-1:main"

    store.mark(key, request_id="one", reason="transport")
    assert store.consume(key)["pending"] is True
    store.mark(key, request_id="two", reason="transport")
    assert store.consume(key)["pending"] is True
    store.mark(key, request_id="three", reason="transport")
    exhausted = store.consume(key)

    assert exhausted["pending"] is False
    assert exhausted["exhausted"] is True
    assert exhausted["failure_count"] == 3


def test_sse_frames_can_be_split_at_arbitrary_tcp_boundaries(tmp_path):
    relay, _store = _relay(tmp_path)
    wire = (
        _message_start()
        + _text_start()
        + _text_delta("split-safe")
        + _block_stop()
        + _message_delta("end_turn")
        + _message_stop()
    )

    output: list[bytes] = []
    for offset in range(0, len(wire), 7):
        output.extend(relay.feed(wire[offset : offset + 7]))

    assert b"".join(output) == wire
    assert relay.message_stopped is True


def test_http_consume_endpoint_uses_claude_session_and_agent_ids(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from headroom.proxy.models import ProxyConfig
    from headroom.proxy.server import create_app

    monkeypatch.setenv(
        "HEADROOM_CLAUDE_STREAM_RECOVERY_DB",
        str(tmp_path / "endpoint-recovery.sqlite3"),
    )
    app = create_app(ProxyConfig())
    key = "claude-code:session-http:agent-7"
    app.state.proxy.claude_stream_recovery.mark(
        key,
        request_id="request-http",
        reason="stream_transport_error",
    )

    client = TestClient(app)
    response = client.post(
        "/__headroom/claude-recovery/consume",
        json={"session_id": "session-http", "agent_id": "agent-7"},
    )
    second = client.post(
        "/__headroom/claude-recovery/consume",
        json={"session_id": "session-http", "agent_id": "agent-7"},
    )

    assert response.status_code == 200
    assert response.json()["pending"] is True
    assert response.json()["request_id"] == "request-http"
    assert second.json()["pending"] is False
