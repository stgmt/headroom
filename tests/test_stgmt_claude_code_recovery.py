import asyncio
import inspect
import pytest

from headroom.memory.config import EmbedderBackend, MemoryConfig
from headroom.memory.factory import _EMBEDDER_CACHE, _create_embedder
from headroom.proxy.handlers import anthropic


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_claude_code_session_key_uses_agent_id() -> None:
    helper = anthropic._headroom_session_header_from_request

    assert helper(_Request({"x-headroom-session-id": "explicit"})) == "explicit"
    assert (
        helper(
            _Request(
                {
                    "x-claude-code-session-id": "session-1",
                    "x-claude-code-agent-id": "agent-2",
                }
            )
        )
        == "claude-code:session-1:agent-2"
    )
    assert helper(_Request({"x-claude-code-session-id": "session-1"})) == "claude-code:session-1:main"
    assert helper(_Request({})) is None


def _compact_prompt() -> str:
    return "\n".join(
        (
            "Your task is to create a detailed summary of the conversation so far.",
            "Wrap the analysis in <analysis> tags and the result in <summary> tags.",
            "Include All user messages, Pending Tasks, Current Work, and Context for Continuing Work.",
        )
    )


def test_claude_code_compact_prompt_is_detected_and_restored() -> None:
    body = {
        "system": "normal Claude Code system prompt",
        "messages": [
            {"role": "user", "content": "normal request"},
            {"role": "assistant", "content": "normal response"},
            {"role": "user", "content": [{"type": "text", "text": _compact_prompt()}]},
        ],
    }

    detected, index = anthropic._is_claude_code_compact_request(body)
    assert detected is True
    assert index == 2

    compressed = [
        body["messages"][0],
        body["messages"][1],
        {"role": "user", "content": "[kompress changed the compact prompt]"},
    ]
    restored = anthropic._restore_claude_code_compact_message(
        compressed, index, body["messages"][index]
    )
    assert restored[2] == body["messages"][2]
    assert compressed[2]["content"] == "[kompress changed the compact prompt]"


def test_post_compact_summary_is_not_misclassified() -> None:
    body = {
        "messages": [
            {
                "role": "user",
                "content": "Summary: Pending Tasks, Current Work, Context for Continuing Work.",
            }
        ]
    }
    assert anthropic._is_claude_code_compact_request(body) == (False, None)


def test_anthropic_mid_turn_path_no_longer_returns_private_202() -> None:
    source = inspect.getsource(anthropic._sub2api_original_handle_anthropic_messages)

    assert "return JSONResponse(content=queued, status_code=202)" not in source
    assert "_wait_for_mid_turn_stream" in source
    assert "_headroom_session_header_from_request(request)" in source


@pytest.mark.asyncio
async def test_handler_watchdog_retries_claude_code_request_with_bypass(monkeypatch) -> None:
    attempts = {"count": 0}

    async def slow_then_retry_ok(self, request, *args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            await asyncio.sleep(3600)
        assert request.headers.get("x-headroom-bypass") == "true"
        assert request.headers.get("x-headroom-mode") == "passthrough"
        assert request.headers.get("x-sub2api-headroom-watchdog-retry") == "1"
        return "retry-ok"

    monkeypatch.setattr(anthropic, "_sub2api_original_handle_anthropic_messages", slow_then_retry_ok)
    monkeypatch.setenv("HEADROOM_CLAUDE_CODE_HANDLER_WATCHDOG_MS", "10")

    response = await anthropic.AnthropicHandlerMixin().handle_anthropic_messages(
        _Request({"x-claude-code-session-id": "verify-session", "x-claude-code-agent-id": "verify-agent"})
    )

    assert response == "retry-ok"
    assert attempts["count"] == 2


def test_embedding_server_socket_selects_socket_embedder(monkeypatch) -> None:
    from headroom.memory.adapters.watchdog import SocketEmbedderClient

    monkeypatch.setenv("HEADROOM_EMBEDDING_SERVER_SOCKET", "/tmp/headroom-test.sock")
    monkeypatch.delenv("HEADROOM_EMBEDDING_SERVER_CHILD", raising=False)
    _EMBEDDER_CACHE.clear()

    embedder = _create_embedder(MemoryConfig(embedder_backend=EmbedderBackend.ONNX))

    assert isinstance(embedder, SocketEmbedderClient)
    assert embedder.socket_path == "/tmp/headroom-test.sock"
