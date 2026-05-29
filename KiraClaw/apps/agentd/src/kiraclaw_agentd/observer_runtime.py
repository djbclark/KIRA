from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kiraclaw_agentd.observer_service import InflightMessageContext, ObserverDecision, ObserverService

logger = logging.getLogger(__name__)


def _snapshot_signature(snapshot: dict[str, Any]) -> str:
    recent_tools = snapshot.get("recent_tool_events") or []
    active_processes = snapshot.get("active_processes") or []
    payload = {
        "state": snapshot.get("state"),
        "prompt": str(snapshot.get("prompt") or "").strip(),
        "streamed_text_tail": str(snapshot.get("streamed_text_tail") or "").strip()[-400:],
        "queued_runs": snapshot.get("queued_runs") or 0,
        "recent_tools": [
            {
                "phase": item.get("phase"),
                "name": item.get("name"),
            }
            for item in recent_tools[-6:]
            if item.get("name")
        ],
        "active_processes": [
            {
                "session_id": item.get("session_id"),
                "status": item.get("status"),
                "command": str(item.get("command") or "").strip()[:160],
            }
            for item in active_processes[:3]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _should_emit_heartbeat(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot.get("run_is_private") or snapshot.get("run_mention"))


async def maybe_route_inflight_message(
    session_manager: Any,
    observer_service: ObserverService | None,
    *,
    session_id: str,
    prompt: str,
    inbound: InflightMessageContext | None = None,
) -> ObserverDecision | None:
    if observer_service is None:
        return None

    has_active_run = getattr(session_manager, "has_active_run", None)
    build_snapshot = getattr(session_manager, "build_observer_snapshot", None)
    if not callable(has_active_run) or not callable(build_snapshot):
        return None
    if not has_active_run(session_id):
        return None

    snapshot = build_snapshot(session_id)
    if not snapshot:
        return None

    return await asyncio.to_thread(observer_service.classify_inflight, prompt, snapshot, inbound)


async def run_heartbeat_loop(
    session_manager: Any,
    observer_service: ObserverService | None,
    *,
    session_id: str,
    run_task: asyncio.Task[Any],
    send_update: Callable[[str], Awaitable[None]],
    initial_delay_seconds: float,
    interval_seconds: float,
) -> None:
    if observer_service is None:
        return

    build_snapshot = getattr(session_manager, "build_observer_snapshot", None)
    if not callable(build_snapshot):
        return

    await asyncio.sleep(max(0.0, float(initial_delay_seconds)))
    last_message = ""
    last_signature = ""
    while not run_task.done():
        snapshot = build_snapshot(session_id)
        if snapshot:
            if not _should_emit_heartbeat(snapshot):
                await asyncio.sleep(max(1.0, float(interval_seconds)))
                continue
            signature = _snapshot_signature(snapshot)
            if signature == last_signature:
                await asyncio.sleep(max(1.0, float(interval_seconds)))
                continue
            try:
                message = await asyncio.to_thread(observer_service.summarize_heartbeat, snapshot)
                text = str(message or "").strip()
                if run_task.done():
                    break
                if text and text != last_message:
                    await send_update(text)
                    last_message = text
                    last_signature = signature
            except Exception as exc:  # pragma: no cover - defensive runtime logging
                logger.warning("Observer heartbeat generation failed for %s: %s", session_id, exc)

        await asyncio.sleep(max(1.0, float(interval_seconds)))


async def cancel_heartbeat_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
