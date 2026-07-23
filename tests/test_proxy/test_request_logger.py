"""Tests for the in-memory request logger.

Covers the `log_full_messages` gate, which controls whether the
pre-compression (`request_messages`) and post-compression
(`compressed_messages`) payloads persist past the in-memory entry onto disk.
Both sides are governed by the same flag so the two sides of the compression
stay in sync - it's pointless to store one without the other.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from headroom.proxy.models import RequestLog
from headroom.proxy.request_logger import RequestLogger


def _entry(**overrides) -> RequestLog:
    base: dict = {
        "request_id": "r1",
        "timestamp": "2026-04-24T10:00:00Z",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens_original": 100,
        "input_tokens_optimized": 40,
        "output_tokens": 10,
        "tokens_saved": 60,
        "savings_percent": 60.0,
        "optimization_latency_ms": 1.0,
        "total_latency_ms": 20.0,
        "tags": {},
        "cache_hit": False,
        "transforms_applied": ["kompress:user:0.4"],
    }
    base.update(overrides)
    return RequestLog(**base)


def test_get_recent_strips_compressed_messages_alongside_request_and_response():
    logger = RequestLogger(log_file=None, log_full_messages=True)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "pre"}],
            compressed_messages=[{"role": "user", "content": "post"}],
            response_content="ok",
        )
    )

    recent = logger.get_recent(10)
    assert len(recent) == 1
    assert "request_messages" not in recent[0]
    assert "compressed_messages" not in recent[0]
    assert "response_content" not in recent[0]


def test_get_recent_with_messages_returns_compressed_messages():
    logger = RequestLogger(log_file=None, log_full_messages=True)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "pre"}],
            compressed_messages=[{"role": "user", "content": "post"}],
        )
    )

    recent = logger.get_recent_with_messages(10)
    assert len(recent) == 1
    assert recent[0]["request_messages"] == [{"role": "user", "content": "pre"}]
    assert recent[0]["compressed_messages"] == [{"role": "user", "content": "post"}]


def test_jsonl_file_strips_both_sides_when_log_full_messages_disabled(tmp_path):
    log_file = tmp_path / "requests.jsonl"
    logger = RequestLogger(log_file=str(log_file), log_full_messages=False)
    logger.log(
        _entry(
            request_messages=[{"role": "user", "content": "pre"}],
            compressed_messages=[{"role": "user", "content": "post"}],
            response_content="ok",
        )
    )

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert "request_messages" not in obj
    assert "compressed_messages" not in obj
    assert "response_content" not in obj


def test_get_memory_stats_accounts_for_compressed_messages():
    logger = RequestLogger(log_file=None)
    logger.log(
        _entry(
            compressed_messages=[{"role": "user", "content": "post"}],
        )
    )

    stats = logger.get_memory_stats()
    assert stats.entry_count == 1
    assert stats.size_bytes > 0


def test_restart_restores_lifetime_history_and_recent_feed_from_jsonl(tmp_path):
    log_file = tmp_path / "requests.jsonl"
    writer = RequestLogger(log_file=str(log_file))
    writer.log(_entry(request_id="r1"))
    writer.log(
        _entry(
            request_id="r2",
            timestamp="2026-04-24T10:01:00Z",
            provider="openai",
            model="gpt-5.6-sol",
            input_tokens_original=200,
            input_tokens_optimized=150,
            output_tokens=20,
            tokens_saved=50,
            optimization_latency_ms=2.0,
            total_latency_ms=40.0,
            cache_hit=True,
            error="upstream failed after logging",
            transforms_applied=["kompress:user:0.4", "tool-search"],
        )
    )

    first_line = log_file.read_text(encoding="utf-8").splitlines()[0]
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(first_line + "\n")
        handle.write("{malformed json\n")

    restored = RequestLogger(log_file=str(log_file))
    history = restored.history_stats()

    assert history["source"] == "jsonl"
    assert history["durable"] is True
    assert history["coverage"] == {
        "completed_requests_only": True,
        "lines_scanned": 4,
        "malformed_lines": 1,
        "duplicate_request_ids": 1,
        "missing_request_ids": 0,
        "unpersisted_requests": 0,
    }
    assert history["requests"] == {
        "total": 2,
        "cached": 1,
        "failed_logged": 1,
        "by_provider": {"anthropic": 1, "openai": 1},
        "by_model": {"claude-sonnet-4-6": 1, "gpt-5.6-sol": 1},
    }
    assert history["tokens"] == {
        "input_original": 300,
        "input_optimized": 190,
        "output": 30,
        "saved": 110,
        "savings_percent": 36.67,
    }
    assert history["latency"] == {
        "total_requests": 2,
        "sum_ms": 60.0,
        "min_ms": 20.0,
        "max_ms": 40.0,
        "average_ms": 30.0,
    }
    assert history["range"] == {
        "first_timestamp": "2026-04-24T10:00:00Z",
        "last_timestamp": "2026-04-24T10:01:00Z",
    }
    assert [row["request_id"] for row in restored.get_recent(10)] == ["r1", "r2"]
    assert restored.stats()["history"]["requests"]["total"] == 2


def test_append_after_truncated_jsonl_line_remains_recoverable(tmp_path):
    log_file = tmp_path / "requests.jsonl"
    payload = asdict(_entry(request_id="r1"))
    log_file.write_text(json.dumps(payload) + "\n{truncated", encoding="utf-8")

    logger = RequestLogger(log_file=str(log_file))
    logger.log(_entry(request_id="r2", timestamp="2026-04-24T10:01:00Z"))

    restarted = RequestLogger(log_file=str(log_file))
    history = restarted.history_stats()
    assert history["requests"]["total"] == 2
    assert history["coverage"]["malformed_lines"] == 1
    assert [row["request_id"] for row in restarted.get_recent(10)] == ["r1", "r2"]
