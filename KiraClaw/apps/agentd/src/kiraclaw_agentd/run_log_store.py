from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any

from kiraclaw_agentd.tool_event_summary import summarize_tool_events

if TYPE_CHECKING:
    from kiraclaw_agentd.session_manager import RunRecord
    from kiraclaw_agentd.settings import KiraClawSettings


def _external_text(record: RunRecord) -> str:
    result = record.result
    if result is None or not result.spoken_messages:
        return ""
    return result.public_response_text


def _silent_reason(record: RunRecord) -> str | None:
    if record.state != "completed":
        return None
    result = record.result
    if result is None or result.spoken_messages:
        return None
    return "no_speak"


_TEXT_FIELD_MAX = 8 * 1024
_EVENT_PAYLOAD_MAX = 2 * 1024
_EVENT_LIST_MAX = 50


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _truncate_event(event: Any) -> Any:
    if not isinstance(event, dict):
        return event
    truncated: dict[str, Any] = {}
    for key, value in event.items():
        if isinstance(value, str) and len(value) > _EVENT_PAYLOAD_MAX:
            truncated[key] = value[:_EVENT_PAYLOAD_MAX] + f"... [truncated {len(value) - _EVENT_PAYLOAD_MAX} chars]"
        else:
            truncated[key] = value
    return truncated


def _truncate_events(events: list[Any]) -> list[Any]:
    if len(events) <= _EVENT_LIST_MAX:
        return [_truncate_event(event) for event in events]
    head = events[: _EVENT_LIST_MAX // 2]
    tail = events[-(_EVENT_LIST_MAX - len(head)) :]
    return [_truncate_event(event) for event in head] + [
        {"type": "truncated", "skipped": len(events) - _EVENT_LIST_MAX}
    ] + [_truncate_event(event) for event in tail]


def build_run_log_entry(record: RunRecord) -> dict[str, Any]:
    result = record.result
    return {
        "run_id": record.run_id,
        "session_id": record.session_id,
        "state": record.state,
        "source": str(record.metadata.get("source", "")),
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "prompt": record.prompt,
        "metadata": record.metadata,
        "internal_summary": result.internal_summary if result else "",
        "spoken_messages": list(result.spoken_messages) if result else [],
        "external_text": _external_text(record),
        "streamed_text": result.streamed_text if result else "",
        "tool_events": list(result.tool_events) if result else [],
        "trace_events": list(result.trace_events) if result else [],
        "tool_summary": summarize_tool_events(result.tool_events if result else []),
        "silent_reason": _silent_reason(record),
        "error": record.error,
    }


def truncate_run_log_entry_for_response(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        **entry,
        "prompt": _truncate_text(entry.get("prompt"), _TEXT_FIELD_MAX),
        "internal_summary": _truncate_text(entry.get("internal_summary"), _TEXT_FIELD_MAX),
        "streamed_text": _truncate_text(entry.get("streamed_text"), _TEXT_FIELD_MAX),
        "tool_events": _truncate_events(list(entry.get("tool_events") or [])),
        "trace_events": _truncate_events(list(entry.get("trace_events") or [])),
    }


class RunLogStore:
    def __init__(self, settings: KiraClawSettings) -> None:
        self._log_dir = settings.run_log_dir or (settings.workspace_dir / "logs")
        self._log_file = settings.run_log_file or (self._log_dir / "runs.jsonl")
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._live_records: dict[str, RunRecord] = {}
        self._sequence = 0

    @property
    def log_file(self) -> Path:
        return self._log_file

    def observe(self, record: RunRecord) -> None:
        with self._lock:
            self._sequence += 1
            if record.state in {"queued", "running"}:
                self._live_records[record.run_id] = record
                self._condition.notify_all()
                return

            self._live_records.pop(record.run_id, None)
            self._append_final_entry(record)
            self._condition.notify_all()

    def append(self, record: RunRecord) -> None:
        with self._lock:
            self._sequence += 1
            self._append_final_entry(record)
            self._condition.notify_all()

    def _append_final_entry(self, record: RunRecord) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        entry = build_run_log_entry(record)
        with self._log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def tail(self, *, limit: int = 50, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            live_rows = self._build_live_rows(session_id=session_id)
        max_rows = max(1, limit)
        scan_target = max(max_rows * 4, 200)
        persisted_rows = self._read_persisted_rows_tail(
            session_id=session_id,
            max_rows=scan_target,
        )
        combined = persisted_rows + live_rows
        combined.sort(key=_sort_run_log_entry_key, reverse=True)
        return [truncate_run_log_entry_for_response(row) for row in combined[:max_rows]]

    def current_sequence(self) -> int:
        with self._lock:
            return int(self._sequence)

    def wait_for_update(self, after_sequence: int, timeout: float = 15.0) -> int | None:
        with self._condition:
            has_new_record = self._condition.wait_for(lambda: self._sequence > int(after_sequence), timeout=timeout)
            if not has_new_record:
                return None
            return int(self._sequence)

    def _build_live_rows(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self._live_records.values():
            if session_id and record.session_id != session_id:
                continue
            rows.append(build_run_log_entry(record))
        return rows

    def _read_persisted_rows_tail(
        self,
        *,
        session_id: str | None = None,
        max_rows: int = 200,
    ) -> list[dict[str, Any]]:
        if not self._log_file.exists():
            return []

        chunk_size = 64 * 1024
        try:
            file_size = self._log_file.stat().st_size
        except OSError:
            return []
        if file_size == 0:
            return []

        collected_lines: list[str] = []
        buffer = b""
        position = file_size
        with self._log_file.open("rb") as handle:
            while position > 0 and len(collected_lines) < max_rows:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                lines = buffer.split(b"\n")
                buffer = lines[0]
                for raw in reversed(lines[1:]):
                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    collected_lines.append(text)
                    if len(collected_lines) >= max_rows:
                        break
            if buffer and len(collected_lines) < max_rows:
                text = buffer.decode("utf-8", errors="replace").strip()
                if text:
                    collected_lines.append(text)

        rows: list[dict[str, Any]] = []
        for line in reversed(collected_lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id and row.get("session_id") != session_id:
                continue
            rows.append(row)
        return rows


def _sort_run_log_entry_key(entry: dict[str, Any]) -> tuple[int, str, str]:
    state = str(entry.get("state") or "").strip().lower()
    priority = {"running": 2, "queued": 1}.get(state, 0)
    if priority > 0:
        timestamp = str(entry.get("started_at") or entry.get("created_at") or "")
    else:
        timestamp = str(entry.get("finished_at") or entry.get("created_at") or "")
    return (priority, timestamp, str(entry.get("run_id") or ""))
