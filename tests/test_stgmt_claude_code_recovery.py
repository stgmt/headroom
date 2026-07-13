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
