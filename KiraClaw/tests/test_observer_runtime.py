from __future__ import annotations

import asyncio

from kiraclaw_agentd.observer_runtime import maybe_route_inflight_message, run_heartbeat_loop
from kiraclaw_agentd.observer_service import InflightMessageContext, ObserverDecision


class _FakeObserverService:
    def __init__(self) -> None:
        self.heartbeat_calls = 0
        self.last_inbound: InflightMessageContext | None = None

    def classify_inflight(
        self,
        prompt: str,
        snapshot: dict,
        inbound: InflightMessageContext | None = None,
    ) -> ObserverDecision:
        self.last_inbound = inbound
        if "어디까지" in prompt:
            return ObserverDecision("status_query", "지금 상태를 보고 있습니다.")
        return ObserverDecision("queue_next", "끝난 뒤 이어서 처리할게요.")

    def summarize_heartbeat(self, snapshot: dict) -> str:
        self.heartbeat_calls += 1
        return f"heartbeat:{snapshot['state']}:{self.heartbeat_calls}"


class _FakeSessionManager:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.snapshots = [
            {"session_id": "slack:C1:main", "state": "running", "run_mention": True, "run_is_private": False},
            {"session_id": "slack:C1:main", "state": "running", "run_mention": True, "run_is_private": False},
        ]

    def has_active_run(self, session_id: str) -> bool:
        return self.active and session_id == "slack:C1:main"

    def build_observer_snapshot(self, session_id: str) -> dict | None:
        if not self.has_active_run(session_id):
            return None
        if self.snapshots:
            return self.snapshots.pop(0)
        return {"session_id": session_id, "state": "running", "run_mention": True, "run_is_private": False}


def test_maybe_route_inflight_message_returns_observer_decision() -> None:
    async def scenario() -> None:
        observer = _FakeObserverService()
        decision = await maybe_route_inflight_message(
            _FakeSessionManager(),
            observer,
            session_id="slack:C1:main",
            prompt="지금 어디까지 했어?",
            inbound=InflightMessageContext(
                source="slack-group",
                mention=True,
                is_private=False,
                user_name="Jiho Jeon",
            ),
        )
        assert decision is not None
        assert decision.intent == "status_query"
        assert observer.last_inbound is not None
        assert observer.last_inbound.mention is True

    asyncio.run(scenario())


def test_run_heartbeat_loop_sends_updates_until_run_finishes() -> None:
    async def scenario() -> None:
        sent: list[str] = []
        observer = _FakeObserverService()

        async def send_update(text: str) -> None:
            sent.append(text)

        async def _finish() -> str:
            await asyncio.sleep(0.08)
            return "done"

        run_task = asyncio.create_task(_finish())
        await run_heartbeat_loop(
            _FakeSessionManager(),
            observer,
            session_id="slack:C1:main",
            run_task=run_task,
            send_update=send_update,
            initial_delay_seconds=0.01,
            interval_seconds=0.02,
        )

        assert sent == ["heartbeat:running:1"]
        assert observer.heartbeat_calls == 1

    asyncio.run(scenario())


def test_run_heartbeat_loop_stays_silent_for_unmentioned_group_runs() -> None:
    async def scenario() -> None:
        sent: list[str] = []
        observer = _FakeObserverService()
        session_manager = _FakeSessionManager()
        session_manager.snapshots = [
            {"session_id": "slack:C1:main", "state": "running", "run_mention": False, "run_is_private": False},
            {"session_id": "slack:C1:main", "state": "running", "run_mention": False, "run_is_private": False},
        ]

        async def send_update(text: str) -> None:
            sent.append(text)

        async def _finish() -> str:
            await asyncio.sleep(0.08)
            return "done"

        run_task = asyncio.create_task(_finish())
        await run_heartbeat_loop(
            session_manager,
            observer,
            session_id="slack:C1:main",
            run_task=run_task,
            send_update=send_update,
            initial_delay_seconds=0.01,
            interval_seconds=0.02,
        )

        assert sent == []
        assert observer.heartbeat_calls == 0

    asyncio.run(scenario())
