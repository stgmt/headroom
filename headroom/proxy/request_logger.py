"""Request logger for the Headroom proxy.

Logs requests to an in-memory deque and optionally to a JSONL file.

Extracted from server.py for maintainability.

Phase G PR-G3 (P4-45): base64-encoded image payloads in the
``request_messages`` / ``response_content`` are redacted before
write to keep request logs small. Multi-MB base64 strings would
otherwise saturate the JSONL log and the in-memory deque.

Remediation (M2, M5): the redactor now ONLY fires inside known
image-bearing JSON paths or against strings that carry an explicit
``data:image/...;base64,`` URL prefix. The earlier "density
heuristic" over-fired on encrypted blobs, signed tokens, minified
JSON, and tool outputs. The replacement placeholder now reports
the UTF-8 byte length under a ``bytes=`` label (was character
length; for the ASCII base64 alphabet the two happen to coincide
but the label is now accurate for any future Unicode payload).
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter, deque
from copy import deepcopy
from dataclasses import asdict, fields
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..memory.tracker import ComponentStats

from headroom.proxy import request_log_redaction_policy
from headroom.proxy.models import RequestLog

IMAGE_BASE64_REDACT_THRESHOLD_BYTES = (
    request_log_redaction_policy.IMAGE_BASE64_REDACT_THRESHOLD_BYTES
)
IMAGE_BASE64_REPLACEMENT_TEMPLATE = request_log_redaction_policy.IMAGE_BASE64_REPLACEMENT_TEMPLATE
IMAGE_BEARING_FIELD_NAMES = request_log_redaction_policy.IMAGE_BEARING_FIELD_NAMES
_is_base64_image_payload = request_log_redaction_policy.is_base64_image_payload

logger = logging.getLogger(__name__)

# Constants for log redaction counter export (Prometheus). The
# Python proxy's ``/metrics`` exporter surfaces
# ``proxy_image_generation_call_log_redacted_total`` from this
# module-level counter. C3 remediation: the Rust proxy previously
# held a dead counter; that's been removed in favour of this
# Python-side counter, which is the natural owner.
_redactions_total: int = 0
_redactions_lock = Lock()
_REQUEST_LOG_FIELDS = {field.name for field in fields(RequestLog)}


def _as_non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_non_negative_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _empty_request_history(log_file: Path | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "jsonl" if log_file else "memory",
        "log_file": str(log_file) if log_file else None,
        "durable": bool(log_file),
        "coverage": {
            "completed_requests_only": True,
            "lines_scanned": 0,
            "malformed_lines": 0,
            "duplicate_request_ids": 0,
            "missing_request_ids": 0,
            "unpersisted_requests": 0,
        },
        "range": {"first_timestamp": None, "last_timestamp": None},
        "requests": {
            "total": 0,
            "cached": 0,
            "failed_logged": 0,
            "by_provider": Counter(),
            "by_model": Counter(),
        },
        "tokens": {
            "input_original": 0,
            "input_optimized": 0,
            "output": 0,
            "saved": 0,
        },
        "latency": {"total_requests": 0, "sum_ms": 0.0, "min_ms": None, "max_ms": 0.0},
        "optimization_latency": {
            "total_requests": 0,
            "sum_ms": 0.0,
            "min_ms": None,
            "max_ms": 0.0,
        },
        "transforms": Counter(),
    }


def redactions_total() -> int:
    """Return the running count of base64 redactions performed.

    Exposed for unit tests, the legacy Python ``/stats`` endpoint,
    and the Prometheus exporter
    (``proxy_image_generation_call_log_redacted_total``).
    """
    with _redactions_lock:
        return _redactions_total


def redact_image_base64(payload: Any) -> Any:
    """Public entry point for base64-image redaction.

    Walks ``payload`` (a dict, list, or string) and replaces any
    over-threshold base64 string with a size-only placeholder.
    Idempotent — applying twice yields the same structure.
    """
    global _redactions_total

    result = request_log_redaction_policy.redact_image_base64_value(payload)
    if result.redactions:
        with _redactions_lock:
            _redactions_total += result.redactions
    return result.value


class RequestLogger:
    """Log requests to JSONL file.

    Uses a deque with max 10,000 entries to prevent unbounded memory growth.
    Gracefully degrades to in-memory-only if the log file cannot be written
    (read-only filesystem, permissions error, etc.).
    """

    MAX_LOG_ENTRIES = 10_000

    def __init__(self, log_file: str | None = None, log_full_messages: bool = False):
        self.log_file = Path(log_file) if log_file else None
        self.log_full_messages = log_full_messages
        # Use deque with maxlen for automatic FIFO eviction
        self._logs: deque[RequestLog] = deque(maxlen=self.MAX_LOG_ENTRIES)
        self._history_lock = Lock()
        self._history = _empty_request_history(self.log_file)
        self._history_request_ids: set[str] = set()
        self._needs_log_separator = False

        if self.log_file:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                self._hydrate_from_jsonl()
            except OSError as e:
                logger.warning(
                    "Cannot create log directory %s: %s — logging to memory only",
                    self.log_file.parent,
                    e,
                )
                self.log_file = None
                self._history = _empty_request_history(None)

    def _hydrate_from_jsonl(self) -> None:
        """Restore lifetime aggregates and the bounded recent feed from JSONL."""
        if self.log_file is None or not self.log_file.exists():
            return

        with self._history_lock:
            with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    self._history["coverage"]["lines_scanned"] += 1
                    try:
                        payload = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        self._history["coverage"]["malformed_lines"] += 1
                        continue
                    if not isinstance(payload, dict):
                        self._history["coverage"]["malformed_lines"] += 1
                        continue
                    if not self._record_history_locked(payload):
                        continue
                    try:
                        restored = RequestLog(
                            **{
                                key: value
                                for key, value in payload.items()
                                if key in _REQUEST_LOG_FIELDS
                            }
                        )
                    except (TypeError, ValueError):
                        continue
                    self._logs.append(restored)

            if self.log_file.stat().st_size > 0:
                with self.log_file.open("rb") as handle:
                    handle.seek(-1, 2)
                    self._needs_log_separator = handle.read(1) not in (b"\n", b"\r")

        logger.info(
            "Request history restored: %s completed requests from %s (%s malformed, %s duplicates)",
            self._history["requests"]["total"],
            self.log_file,
            self._history["coverage"]["malformed_lines"],
            self._history["coverage"]["duplicate_request_ids"],
        )

    def _record_history_locked(self, payload: dict[str, Any]) -> bool:
        request_id = str(payload.get("request_id") or "").strip()
        if request_id:
            if request_id in self._history_request_ids:
                self._history["coverage"]["duplicate_request_ids"] += 1
                return False
            self._history_request_ids.add(request_id)
        else:
            self._history["coverage"]["missing_request_ids"] += 1

        requests = self._history["requests"]
        requests["total"] += 1
        requests["cached"] += int(bool(payload.get("cache_hit")))
        requests["failed_logged"] += int(bool(payload.get("error")))
        provider = str(payload.get("provider") or "unknown")
        model = str(payload.get("model") or "unknown")
        requests["by_provider"][provider] += 1
        requests["by_model"][model] += 1

        tokens = self._history["tokens"]
        tokens["input_original"] += _as_non_negative_int(payload.get("input_tokens_original"))
        tokens["input_optimized"] += _as_non_negative_int(payload.get("input_tokens_optimized"))
        tokens["output"] += _as_non_negative_int(payload.get("output_tokens"))
        tokens["saved"] += _as_non_negative_int(payload.get("tokens_saved"))

        self._record_latency_locked("latency", payload.get("total_latency_ms"))
        self._record_latency_locked(
            "optimization_latency", payload.get("optimization_latency_ms")
        )
        for transform in payload.get("transforms_applied") or []:
            name = str(transform).strip()
            if name:
                self._history["transforms"][name] += 1

        timestamp = str(payload.get("timestamp") or "").strip()
        if timestamp:
            request_range = self._history["range"]
            first = request_range["first_timestamp"]
            last = request_range["last_timestamp"]
            request_range["first_timestamp"] = min(first, timestamp) if first else timestamp
            request_range["last_timestamp"] = max(last, timestamp) if last else timestamp
        return True

    def _record_latency_locked(self, key: str, value: Any) -> None:
        latency = _as_non_negative_float(value)
        if latency is None:
            return
        stats = self._history[key]
        stats["total_requests"] += 1
        stats["sum_ms"] += latency
        stats["min_ms"] = latency if stats["min_ms"] is None else min(stats["min_ms"], latency)
        stats["max_ms"] = max(stats["max_ms"], latency)

    def history_stats(self) -> dict[str, Any]:
        """Return JSON-safe lifetime request statistics restored from the log."""
        with self._history_lock:
            snapshot = deepcopy(self._history)
        for key in ("latency", "optimization_latency"):
            block = snapshot[key]
            count = block["total_requests"]
            block["average_ms"] = round(block["sum_ms"] / count, 2) if count else 0.0
            block["sum_ms"] = round(block["sum_ms"], 2)
            block["min_ms"] = round(block["min_ms"], 2) if block["min_ms"] is not None else 0.0
            block["max_ms"] = round(block["max_ms"], 2)

        tokens = snapshot["tokens"]
        tokens["savings_percent"] = round(
            (tokens["saved"] / tokens["input_original"] * 100)
            if tokens["input_original"]
            else 0.0,
            2,
        )
        for key in ("by_provider", "by_model"):
            snapshot["requests"][key] = dict(snapshot["requests"][key].most_common())
        snapshot["transforms"] = dict(snapshot["transforms"].most_common())
        return snapshot

    def log(self, entry: RequestLog):
        """Log a request. Oldest entries are automatically removed when limit reached.

        Phase G PR-G3 (P4-45): base64-encoded image payloads in
        ``request_messages`` / ``compressed_messages`` / ``response_content``
        are redacted before write. Redaction also applies to the in-memory
        deque so the ``/stats/recent_requests`` endpoint never serves a
        multi-MB image either.
        """
        # Redact image payloads in-place on the deque entry so memory
        # use stays bounded. We mutate the dataclass fields rather
        # than wrapping the entry to keep ``get_recent`` /
        # ``get_recent_with_messages`` unchanged.
        if entry.request_messages is not None:
            entry.request_messages = redact_image_base64(entry.request_messages)
        if entry.compressed_messages is not None:
            entry.compressed_messages = redact_image_base64(entry.compressed_messages)
        if entry.response_content is not None:
            entry.response_content = redact_image_base64(entry.response_content)

        log_dict = asdict(entry)
        persisted = False
        with self._history_lock:
            self._logs.append(entry)
            if self.log_file:
                try:
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        if self._needs_log_separator:
                            f.write("\n")
                            self._needs_log_separator = False
                        disk_dict = dict(log_dict)
                        if not self.log_full_messages:
                            disk_dict.pop("request_messages", None)
                            disk_dict.pop("compressed_messages", None)
                            disk_dict.pop("response_content", None)
                        f.write(json.dumps(disk_dict) + "\n")
                        persisted = True
                        self._history["coverage"]["lines_scanned"] += 1
                except OSError:
                    pass  # Graceful degradation: memory-only logging continues
            if not persisted and self.log_file:
                self._history["coverage"]["unpersisted_requests"] += 1
            self._record_history_locked(log_dict)

    def get_recent(self, n: int = 100) -> list[dict]:
        """Get recent log entries (without request/compressed messages and response_content)."""
        # Convert deque to list for slicing (deque doesn't support slicing)
        with self._history_lock:
            entries = list(self._logs)[-n:]
        return [
            {
                k: v
                for k, v in asdict(e).items()
                if k not in ("request_messages", "compressed_messages", "response_content")
            }
            for e in entries
        ]

    def get_recent_with_messages(self, n: int = 20) -> list[dict]:
        """Get recent log entries including full request/response messages."""
        with self._history_lock:
            entries = list(self._logs)[-n:]
        return [asdict(e) for e in entries]

    def stats(self) -> dict:
        """Get logging statistics."""
        return {
            "total_logged": len(self._logs),
            "log_file": str(self.log_file) if self.log_file else None,
            "history": self.history_stats(),
        }

    def get_memory_stats(self) -> ComponentStats:
        """Get memory statistics for the MemoryTracker.

        Returns:
            ComponentStats with current memory usage.
        """
        from ..memory.tracker import ComponentStats

        # Calculate size
        with self._history_lock:
            log_entries = list(self._logs)
            history_ids_size = sys.getsizeof(self._history_request_ids) + sum(
                len(request_id) for request_id in self._history_request_ids
            )
        size_bytes = sys.getsizeof(log_entries) + history_ids_size

        for log_entry in log_entries:
            size_bytes += sys.getsizeof(log_entry)
            # Add string fields
            if log_entry.request_id:
                size_bytes += len(log_entry.request_id)
            if log_entry.provider:
                size_bytes += len(log_entry.provider)
            if log_entry.model:
                size_bytes += len(log_entry.model)
            if log_entry.error:
                size_bytes += len(log_entry.error)
            # Messages and response can be large
            if log_entry.request_messages:
                size_bytes += sys.getsizeof(log_entry.request_messages)
            if log_entry.compressed_messages:
                size_bytes += sys.getsizeof(log_entry.compressed_messages)
            if log_entry.response_content:
                size_bytes += len(log_entry.response_content)

        return ComponentStats(
            name="request_logger",
            entry_count=len(log_entries),
            size_bytes=size_bytes,
            budget_bytes=None,
            hits=0,
            misses=0,
            evictions=0,
        )
