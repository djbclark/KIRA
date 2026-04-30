from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from kiraclaw_agentd.engine import _ensure_provider_credentials, create_model
from kiraclaw_agentd.settings import KiraClawSettings


@dataclass
class ObserverDecision:
    intent: str
    reply_text: str


@dataclass(frozen=True)
class InflightMessageContext:
    source: str = ""
    mention: bool = False
    is_private: bool = False
    user_name: str = ""


def _clip(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _format_snapshot(snapshot: dict[str, Any]) -> str:
    lines = [
        f"session_id: {snapshot.get('session_id') or ''}",
        f"source: {snapshot.get('source') or ''}",
        f"run_mention: {str(bool(snapshot.get('run_mention'))).lower()}",
        f"run_is_private: {str(bool(snapshot.get('run_is_private'))).lower()}",
        f"state: {snapshot.get('state') or ''}",
        f"elapsed_seconds: {snapshot.get('elapsed_seconds') or 0}",
        f"queued_runs: {snapshot.get('queued_runs') or 0}",
        f"current_request: {_clip(str(snapshot.get('prompt') or ''), 300)}",
    ]

    stream_tail = _clip(str(snapshot.get("streamed_text_tail") or ""), 600)
    if stream_tail:
        lines.append(f"stream_tail: {stream_tail}")

    recent_tools = snapshot.get("recent_tool_events") or []
    if recent_tools:
        formatted = ", ".join(
            _clip(f"{item.get('phase')}:{item.get('name')}", 60)
            for item in recent_tools
            if item.get("name")
        )
        if formatted:
            lines.append(f"recent_tools: {formatted}")

    active_processes = snapshot.get("active_processes") or []
    if active_processes:
        summaries: list[str] = []
        for item in active_processes[:2]:
            command = _clip(str(item.get("command") or ""), 120)
            status = str(item.get("status") or "unknown")
            summaries.append(f"{item.get('session_id')}: {status}: {command}")
        if summaries:
            lines.append(f"active_processes: {' | '.join(summaries)}")

    return "\n".join(lines)


def _format_inflight_context(context: InflightMessageContext | None) -> str:
    if context is None:
        context = InflightMessageContext()
    lines = [
        f"incoming_source: {context.source}",
        f"incoming_mention: {str(bool(context.mention)).lower()}",
        f"incoming_is_private: {str(bool(context.is_private)).lower()}",
        f"incoming_user_name: {_clip(context.user_name, 80)}",
    ]
    return "\n".join(lines)


class ObserverService:
    def __init__(
        self,
        settings: KiraClawSettings,
        *,
        model_factory: Callable[[str, str | None, int], Any] | None = None,
        credential_checker: Callable[[KiraClawSettings, str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._model_factory = model_factory or create_model
        self._credential_checker = credential_checker or _ensure_provider_credentials

    def _active_name(self) -> str:
        return (self.settings.agent_name or "").strip() or "KIRA"

    def classify_inflight(
        self,
        user_message: str,
        snapshot: dict[str, Any],
        inbound: InflightMessageContext | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> ObserverDecision:
        fallback = self._fallback_classification(user_message, snapshot, inbound)
        if self._should_suppress_queue_ack(inbound):
            return ObserverDecision(intent="queue_next", reply_text="")
        active_name = self._active_name()
        try:
            parsed = self._chat_json(
                system_prompt=(
                    f"You are {active_name}, replying in read-only status mode for your own in-progress run.\n"
                    "From the user's perspective, you and the main run are one agent.\n"
                    "You cannot do work, call tools, or modify the current task.\n"
                    "Choose exactly one intent:\n"
                    "- status_query: the user is asking what is happening, progress, status, timing, or why it is taking time\n"
                    "- queue_next: the message is a new follow-up task that should wait until the current run completes\n"
                    "- unsupported_control: the user wants to cancel, stop, interrupt, reprioritize, or steer the current run\n"
                    "Return strict JSON only: {\"intent\":\"...\",\"reply_text\":\"...\"}\n"
                    "reply_text rules:\n"
                    "- status_query: answer from the snapshot only, in one or two short sentences\n"
                    "- status_query: describe the current work in plain language and, if clear, the next visible step\n"
                    "- status_query: do not mention elapsed time unless the user explicitly asked about timing\n"
                    "- status_query: do not speculate about motives, silence, direct requests, hidden reasoning, or why the run chose its current path\n"
                    "- status_query: do not mention internal tools or implementation details unless they clearly help the user understand the current task\n"
                    "- shared-room norms depend on the incoming message context, not just the active run source\n"
                    "- if incoming_is_private is false, keep shared-room norms in mind\n"
                    "- if incoming_mention is true, the user explicitly addressed you\n"
                    "- if incoming_is_private is false and incoming_mention is false, reply only when the message clearly asks about the in-progress work; otherwise prefer queue_next\n"
                    "- if incoming_is_private is false, keep the reply brief and non-disruptive\n"
                    "- queue_next: briefly acknowledge that the new request will wait until the current run completes\n"
                    "- unsupported_control: briefly say that the current in-progress run cannot be modified or canceled yet\n"
                    "Never invent progress beyond the snapshot."
                ),
                user_prompt=(
                    f"Incoming user message:\n{_clip(user_message, 400)}\n\n"
                    f"Incoming message context:\n{_format_inflight_context(inbound)}\n\n"
                    f"Current run snapshot:\n{_format_snapshot(snapshot)}"
                ),
                provider=provider,
                model=model,
            )
        except Exception:
            return fallback
        if not parsed:
            return fallback

        intent = str(parsed.get("intent") or "").strip().lower()
        reply_text = str(parsed.get("reply_text") or "").strip()
        if intent not in {"status_query", "queue_next", "unsupported_control"}:
            return fallback
        if not reply_text:
            reply_text = fallback.reply_text
        if intent == "queue_next" and self._should_suppress_queue_ack(inbound):
            reply_text = ""
        return ObserverDecision(intent=intent, reply_text=reply_text)

    def summarize_heartbeat(
        self,
        snapshot: dict[str, Any],
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        fallback = self._fallback_heartbeat(snapshot)
        active_name = self._active_name()
        try:
            parsed = self._chat_json(
                system_prompt=(
                    f"You are {active_name}, replying in read-only status mode for your own in-progress run.\n"
                    "From the user's perspective, you and the main run are one agent.\n"
                    "Write one short user-facing progress update from the snapshot only.\n"
                    "Keep it to one or two short sentences.\n"
                    "Describe the current work in plain language.\n"
                    "If helpful, mention the next visible step.\n"
                    "Do not mention elapsed time unless the user explicitly asked about timing.\n"
                    "Do not speculate about motives, silence, direct requests, or why the run is taking its current path.\n"
                    "Do not mention internal implementation details unless they clearly help.\n"
                    "run_mention and run_is_private describe how the current run was invoked.\n"
                    "If run_is_private is false, keep shared-room norms in mind.\n"
                    "If run_is_private is false and run_mention is false, keep the update especially brief and low-noise.\n"
                    "If source ends with '-group', keep the update especially brief and low-noise.\n"
                    "Do not promise completion times.\n"
                    "Return strict JSON only: {\"reply_text\":\"...\"}"
                ),
                user_prompt=f"Current run snapshot:\n{_format_snapshot(snapshot)}",
                provider=provider,
                model=model,
            )
        except Exception:
            return fallback
        if not parsed:
            return fallback
        reply_text = str(parsed.get("reply_text") or "").strip()
        return reply_text or fallback

    def _chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider: str | None,
        model: str | None,
    ) -> dict[str, Any] | None:
        selected_provider = provider or self.settings.provider
        selected_model = model or self.settings.model
        self._credential_checker(self.settings, selected_provider)
        model_impl = self._model_factory(
            selected_provider,
            selected_model,
            min(self.settings.max_tokens, 512),
        )
        response = model_impl.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=[],
        )
        return _extract_json_object(response.text or "")

    def _fallback_classification(
        self,
        user_message: str,
        snapshot: dict[str, Any],
        inbound: InflightMessageContext | None = None,
    ) -> ObserverDecision:
        context = inbound or InflightMessageContext()
        message = str(user_message or "").strip()
        lowered = message.lower()
        if any(token in lowered for token in ["cancel", "stop", "pause", "interrupt"]):
            return ObserverDecision(
                intent="unsupported_control",
                reply_text="The current in-progress task cannot be changed or canceled yet. I'll pick it up after this run finishes.",
            )
        if any(token in message for token in ["취소", "중단", "멈춰", "그만", "바꿔", "대신", "말고"]):
            return ObserverDecision(
                intent="unsupported_control",
                reply_text="The current in-progress task cannot be changed or canceled yet. I'll pick it up after this run finishes.",
            )
        if any(token in lowered for token in ["status", "progress", "doing", "how long", "what are you", "still"]):
            return ObserverDecision(intent="status_query", reply_text=self._fallback_heartbeat(snapshot))
        if any(token in message for token in ["상태", "진행", "어디까지", "뭐 하고", "얼마나", "어떻게 됐"]):
            return ObserverDecision(intent="status_query", reply_text=self._fallback_heartbeat(snapshot))
        return ObserverDecision(
            intent="queue_next",
            reply_text="" if self._should_suppress_queue_ack(context) else "I'll handle that after the current task finishes.",
        )

    def _fallback_heartbeat(self, snapshot: dict[str, Any]) -> str:
        active_processes = snapshot.get("active_processes") or []
        if active_processes:
            return f"{self._active_name()} is still working on the current task. I'll follow up when there's something useful to share."

        prompt = _clip(str(snapshot.get("prompt") or ""), 120)
        if prompt:
            return f"{self._active_name()} is working on the current request. I'll follow up once this step is done."
        return f"{self._active_name()} is still working on the current task."

    @staticmethod
    def _should_suppress_queue_ack(context: InflightMessageContext | None) -> bool:
        if context is None:
            return False
        return not context.is_private and not context.mention
