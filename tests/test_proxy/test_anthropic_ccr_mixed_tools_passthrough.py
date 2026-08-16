"""Mixed-tools CCR buffered responses must pass through, not fail closed with 502.

Regression test for the incident where a model called ``headroom_retrieve``
alongside a non-CCR tool in a buffered (``stream:true`` but CCR-buffered)
turn. The CCR handler cannot build a valid continuation without results for
the other tools, so it intentionally skips and the client must resolve all
tool calls itself. The proxy must return the buffered response as-is (200)
instead of killing the agent turn with a fail-closed 502.

A residual CCR-only call (max rounds hit / true failure) must still fail
closed with 502.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from headroom.proxy.server import ProxyConfig, create_app  # noqa: E402


def _make_config() -> ProxyConfig:
    return ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        ccr_inject_tool=True,
        ccr_handle_responses=True,
        ccr_context_tracking=False,
        image_optimize=False,
    )


_RETRIEVE_TOOL = {
    "name": "headroom_retrieve",
    "description": "Retrieve compressed content",
    "input_schema": {"type": "object", "properties": {"hash": {"type": "string"}}},
}


def _request_payload() -> dict:
    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 64,
        "stream": True,
        "tools": [_RETRIEVE_TOOL],
        "messages": [{"role": "user", "content": "hi"}],
    }


def _mixed_tool_response() -> dict:
    """Anthropic-shaped response with headroom_retrieve + one non-CCR tool."""
    return {
        "id": "msg_ccr_mixed",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_retrieve",
                "name": "headroom_retrieve",
                "input": {"hash": "abc123def456"},
            },
            {
                "type": "tool_use",
                "id": "toolu_bash",
                "name": "bash",
                "input": {"command": "ls"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 50,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def _ccr_only_response() -> dict:
    """Anthropic-shaped response with only a headroom_retrieve tool use."""
    return {
        "id": "msg_ccr_only",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_retrieve",
                "name": "headroom_retrieve",
                "input": {"hash": "abc123def456"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 50,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def _exercise(tool_resp: dict) -> httpx.Response:
    with patch("headroom.proxy.server.AnyLLMBackend"):
        app = create_app(_make_config())
        with TestClient(app) as client:
            proxy = client.app.state.proxy

            async def _fake_retry(method, url, headers, body, stream=False, **kwargs):
                return httpx.Response(200, json=tool_resp)

            proxy._retry_request = _fake_retry

            skip_handler = MagicMock()
            skip_handler.has_ccr_tool_calls = MagicMock(return_value=True)
            skip_handler.handle_response = AsyncMock(return_value=tool_resp)
            proxy.ccr_response_handler = skip_handler

            return client.post(
                "/v1/messages",
                headers={
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                    "user-agent": "python-sdk/1.0",
                },
                json=_request_payload(),
            )


def _parse_sse_tool_names(text: str) -> list[str]:
    """Collect tool_use names from an Anthropic SSE response body."""
    names: list[str] = []
    for raw_event in text.split("event: "):
        if "data: " not in raw_event:
            continue
        data_part = raw_event.split("data: ", 1)[1].split("\n\n", 1)[0]
        try:
            import json as _json

            payload = _json.loads(data_part)
        except ValueError:
            continue
        if payload.get("type") != "content_block_start":
            continue
        block = payload.get("content_block") or {}
        if block.get("type") == "tool_use" and block.get("name"):
            names.append(block["name"])
    return names


def test_anthropic_ccr_mixed_tools_passes_through_not_502():
    """Mixed tools (CCR + non-CCR) must be returned to the client as 200."""
    resp = _exercise(_mixed_tool_response())

    assert resp.status_code == 200, (
        f"mixed-tools buffered CCR must pass through as 200, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )
    names = _parse_sse_tool_names(resp.text)
    assert "headroom_retrieve" in names, "client must receive headroom_retrieve to resolve"
    assert "bash" in names, "client must receive the non-CCR tool call to resolve"


def test_anthropic_ccr_only_residual_still_fails_closed():
    """A residual CCR-only call (max rounds hit) must still fail closed."""
    resp = _exercise(_ccr_only_response())

    assert resp.status_code == 502, (
        f"residual CCR-only call must still fail closed, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )
    # The fail-closed path streams an SSE error event; the client must never
    # see a raw headroom_retrieve tool call.
    assert "event: error" in resp.text, f"expected SSE error event, got: {resp.text[:200]}"
    assert "headroom_retrieve" not in resp.text, (
        "proxy silently forwarded the raw CCR tool call"
    )
