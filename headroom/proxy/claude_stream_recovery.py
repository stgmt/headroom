"""Claude Code stream checkpointing and same-session recovery state.

The relay keeps ordinary text streaming live, but holds a tool-use content
block until its closing event arrives. If an Anthropic SSE stream fails after
``message_start``, it emits a valid terminal checkpoint instead of forwarding
``event: error``. A Claude Code Stop hook can then consume the persisted marker
and request one more model turn in the same session.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from headroom import paths as headroom_paths


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class ClaudeStreamRecoveryStore:
    """Small SQLite ledger shared by stream workers and host hooks."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = 900,
        max_attempts: int = 3,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claude_stream_recovery (
                    session_key TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    failure_count INTEGER NOT NULL,
                    pending INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )

    @classmethod
    def from_environment(cls, *, stateless: bool = False) -> ClaudeStreamRecoveryStore:
        enabled = _env_bool("HEADROOM_CLAUDE_STREAM_RECOVERY", True)
        ttl = int(os.environ.get("HEADROOM_CLAUDE_STREAM_RECOVERY_TTL_SECONDS", "900"))
        attempts = int(os.environ.get("HEADROOM_CLAUDE_STREAM_RECOVERY_MAX_ATTEMPTS", "3"))
        configured_path = os.environ.get("HEADROOM_CLAUDE_STREAM_RECOVERY_DB", "").strip()
        path: str | Path
        if stateless:
            path = ":memory:"
        elif configured_path:
            path = configured_path
        else:
            path = headroom_paths.ensure_workspace_dir() / "claude-stream-recovery.sqlite3"
        return cls(path, ttl_seconds=ttl, max_attempts=attempts, enabled=enabled)

    def _purge_expired(self, now: float) -> None:
        self._connection.execute(
            "DELETE FROM claude_stream_recovery WHERE expires_at <= ?",
            (now,),
        )

    def mark(self, session_key: str, *, request_id: str, reason: str) -> dict[str, Any]:
        if not self.enabled:
            return {"pending": False, "enabled": False}
        now = time.time()
        expires_at = now + self.ttl_seconds
        with self._lock, self._connection:
            self._purge_expired(now)
            row = self._connection.execute(
                "SELECT failure_count FROM claude_stream_recovery WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            failure_count = int(row["failure_count"]) + 1 if row else 1
            self._connection.execute(
                """
                INSERT INTO claude_stream_recovery (
                    session_key, request_id, reason, failure_count, pending,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    request_id = excluded.request_id,
                    reason = excluded.reason,
                    failure_count = excluded.failure_count,
                    pending = 1,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    session_key,
                    request_id,
                    reason,
                    failure_count,
                    now,
                    now,
                    expires_at,
                ),
            )
        return {
            "pending": failure_count <= self.max_attempts,
            "exhausted": failure_count > self.max_attempts,
            "failure_count": failure_count,
            "max_attempts": self.max_attempts,
        }

    def consume(self, session_key: str) -> dict[str, Any]:
        if not self.enabled:
            return {"pending": False, "enabled": False}
        now = time.time()
        with self._lock, self._connection:
            self._purge_expired(now)
            row = self._connection.execute(
                """
                SELECT request_id, reason, failure_count, pending, created_at, expires_at
                FROM claude_stream_recovery
                WHERE session_key = ?
                """,
                (session_key,),
            ).fetchone()
            if row is None or not bool(row["pending"]):
                return {"pending": False, "exhausted": False}
            failure_count = int(row["failure_count"])
            self._connection.execute(
                "UPDATE claude_stream_recovery SET pending = 0, updated_at = ? WHERE session_key = ?",
                (now, session_key),
            )
        if failure_count > self.max_attempts:
            return {
                "pending": False,
                "exhausted": True,
                "failure_count": failure_count,
                "max_attempts": self.max_attempts,
            }
        return {
            "pending": True,
            "exhausted": False,
            "request_id": row["request_id"],
            "reason": row["reason"],
            "failure_count": failure_count,
            "max_attempts": self.max_attempts,
            "expires_at": row["expires_at"],
        }

    def clear(self, session_key: str) -> None:
        if not self.enabled:
            return
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM claude_stream_recovery WHERE session_key = ?",
                (session_key,),
            )

    def stats(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "tracked_sessions": 0,
                "pending_sessions": 0,
                "exhausted_sessions": 0,
            }
        now = time.time()
        with self._lock, self._connection:
            self._purge_expired(now)
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS tracked,
                    SUM(CASE WHEN pending = 1 AND failure_count <= ? THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN failure_count > ? THEN 1 ELSE 0 END) AS exhausted
                FROM claude_stream_recovery
                """,
                (self.max_attempts, self.max_attempts),
            ).fetchone()
        return {
            "enabled": True,
            "tracked_sessions": int(row["tracked"] or 0),
            "pending_sessions": int(row["pending"] or 0),
            "exhausted_sessions": int(row["exhausted"] or 0),
            "max_attempts": self.max_attempts,
            "ttl_seconds": self.ttl_seconds,
        }


class AnthropicCheckpointRelay:
    """Incremental Anthropic SSE relay with atomic tool-use boundaries."""

    _TOOL_BLOCK_TYPES = {"tool_use", "server_tool_use"}

    def __init__(
        self,
        *,
        store: ClaudeStreamRecoveryStore,
        session_key: str,
        request_id: str,
    ) -> None:
        self.store = store
        self.session_key = session_key
        self.request_id = request_id
        self._buffer = bytearray()
        self._open_blocks: dict[int, str] = {}
        self._held_tool_frames: dict[int, list[bytes]] = {}
        self._deferred_terminal_frames: list[bytes] = []
        self._message_started = False
        self._message_delta_sent = False
        self._stop_reason: str | None = None
        self._completed_tool_use = False
        self._emitted_content_block = False
        self.message_stopped = False
        self.checkpointed = False
        self.checkpoint_reason: str | None = None
        self.terminal = False

    @property
    def message_started(self) -> bool:
        return self._message_started

    @staticmethod
    def _frame_boundary(buffer: bytearray) -> tuple[int, int] | None:
        candidates: list[tuple[int, int]] = []
        lf = buffer.find(b"\n\n")
        if lf >= 0:
            candidates.append((lf, 2))
        crlf = buffer.find(b"\r\n\r\n")
        if crlf >= 0:
            candidates.append((crlf, 4))
        return min(candidates, default=None, key=lambda item: item[0])

    @staticmethod
    def _parse_frame(frame: bytes) -> tuple[str, dict[str, Any] | None]:
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError:
            return "", None
        event_name = ""
        data_lines: list[str] = []
        for raw_line in text.replace("\r\n", "\n").split("\n"):
            if raw_line.startswith("event:"):
                event_name = raw_line[6:].strip()
            elif raw_line.startswith("data:"):
                data_lines.append(raw_line[5:].lstrip())
        if not data_lines:
            return event_name, None
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return event_name, None
        return event_name, payload if isinstance(payload, dict) else None

    @staticmethod
    def _event(event: str, payload: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()

    def feed(self, chunk: bytes) -> list[bytes]:
        if self.terminal or not chunk:
            return []
        self._buffer.extend(chunk)
        output: list[bytes] = []
        while not self.terminal and (boundary := self._frame_boundary(self._buffer)) is not None:
            index, delimiter_size = boundary
            frame_size = index + delimiter_size
            frame = bytes(self._buffer[:frame_size])
            del self._buffer[:frame_size]
            output.extend(self._process_frame(frame))
        return output

    def _process_frame(self, frame: bytes) -> list[bytes]:
        event_name, payload = self._parse_frame(frame)
        event_type = payload.get("type", "") if payload else ""

        if event_name == "error" or event_type == "error":
            if not self._message_started:
                self.terminal = True
                return [frame]
            error = payload.get("error", {}) if payload else {}
            reason = str(error.get("type") or "upstream_sse_error")
            return self._checkpoint(reason)

        if event_type == "message_start":
            self._message_started = True
            return [frame]

        if event_type == "content_block_start":
            index = int(payload.get("index", 0))
            block = payload.get("content_block", {})
            block_type = str(block.get("type", "")) if isinstance(block, dict) else ""
            self._open_blocks[index] = block_type
            if block_type in self._TOOL_BLOCK_TYPES:
                self._held_tool_frames[index] = [frame]
                return []
            self._emitted_content_block = True
            return [frame]

        if event_type == "content_block_delta":
            index = int(payload.get("index", 0))
            if index in self._held_tool_frames:
                self._held_tool_frames[index].append(frame)
                return []
            return [frame]

        if event_type == "content_block_stop":
            index = int(payload.get("index", 0))
            self._open_blocks.pop(index, None)
            if index in self._held_tool_frames:
                held = self._held_tool_frames.pop(index)
                held.append(frame)
                self._completed_tool_use = True
                self._emitted_content_block = True
                output = held
                if not self._held_tool_frames and self._deferred_terminal_frames:
                    output.extend(self._deferred_terminal_frames)
                    self._deferred_terminal_frames.clear()
                return output
            return [frame]

        if event_type == "message_delta":
            delta = payload.get("delta", {})
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    self._stop_reason = stop_reason
            if self._held_tool_frames:
                self._deferred_terminal_frames.append(frame)
                return []
            self._message_delta_sent = True
            return [frame]

        if event_type == "message_stop":
            if self._held_tool_frames:
                return self._checkpoint("incomplete_tool_use")
            self.message_stopped = True
            self.terminal = True
            self.store.clear(self.session_key)
            return [frame]

        return [frame]

    def finish(self, reason: str) -> list[bytes]:
        if self.terminal:
            return []
        if not self._message_started:
            return []
        return self._checkpoint(reason)

    def _checkpoint(self, reason: str) -> list[bytes]:
        if self.terminal:
            return []

        output: list[bytes] = []
        incomplete_tool_indexes = set(self._held_tool_frames)
        self._held_tool_frames.clear()
        self._deferred_terminal_frames.clear()

        for index in sorted(self._open_blocks):
            if index in incomplete_tool_indexes:
                continue
            output.append(
                self._event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": index},
                )
            )
        self._open_blocks.clear()

        if not self._emitted_content_block:
            output.extend(
                [
                    self._event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    ),
                    self._event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": 0},
                    ),
                ]
            )

        needs_hook = self._stop_reason is None and not self._completed_tool_use
        stop_reason = self._stop_reason or ("tool_use" if self._completed_tool_use else "end_turn")
        if not self._message_delta_sent:
            output.append(
                self._event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                        "usage": {"output_tokens": 0},
                    },
                )
            )
        output.append(self._event("message_stop", {"type": "message_stop"}))

        if needs_hook:
            self.store.mark(
                self.session_key,
                request_id=self.request_id,
                reason=reason,
            )
        else:
            self.store.clear(self.session_key)

        self.checkpointed = True
        self.checkpoint_reason = reason
        self.message_stopped = True
        self.terminal = True
        return output
