from __future__ import annotations

from headroom.proxy.handlers.anthropic import (
    _memory_continuation_content_for_results,
    _without_server_memory_tools,
)


def test_memory_continuation_defers_client_owned_tool_calls() -> None:
    content = [
        {"type": "text", "text": "I will check both sources."},
        {
            "type": "tool_use",
            "id": "mem_1",
            "name": "memory_search",
            "input": {"query": "project location"},
        },
        {
            "type": "tool_use",
            "id": "client_1",
            "name": "Bash",
            "input": {"command": "find /home -maxdepth 2 -type d"},
        },
    ]
    tool_results = [{"type": "tool_result", "tool_use_id": "mem_1", "content": "memory result"}]

    continuation, deferred, unmatched = _memory_continuation_content_for_results(
        content,
        tool_results,
    )

    continuation_calls = [block for block in continuation if block.get("type") == "tool_use"]
    assert [block["id"] for block in continuation_calls] == ["mem_1"]
    assert [block["id"] for block in deferred] == ["client_1"]
    assert unmatched == []
    assert content[2]["id"] == "client_1"


def test_memory_continuation_reports_unmatched_results() -> None:
    continuation, deferred, unmatched = _memory_continuation_content_for_results(
        [{"type": "text", "text": "No tool call was returned."}],
        [{"type": "tool_result", "tool_use_id": "missing_1", "content": "result"}],
    )

    assert continuation == [{"type": "text", "text": "No tool call was returned."}]
    assert deferred == []
    assert unmatched == ["missing_1"]


def test_memory_continuation_removes_private_memory_tool_definitions() -> None:
    tools = [
        {"name": "memory_search", "input_schema": {"type": "object"}},
        {"name": "Bash", "input_schema": {"type": "object"}},
        {"type": "memory_20250818", "name": "memory"},
    ]

    assert _without_server_memory_tools(tools) == [
        {"name": "Bash", "input_schema": {"type": "object"}}
    ]
